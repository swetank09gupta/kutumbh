"""
The home loan, reconciled against the lender and looked forward.

Evidence only: each HDFC prepayment letter states the principal, EMI, rate and EMIs left after that prepayment. The
roll-forward (app.engine.loan) must reproduce the next letter's figure; what it cannot reproduce is reported with its
size, never smoothed over. Forecasts are labelled as forecasts and are replaced by the next letter.

    reconcile()   each interval between two letters: reproduced to the rupee, or by what it differs and why
    report()      balance, rate, EMI, payoff, interest saved by prepayments, the repo-linked rate forecast
    tax()         interest and principal of the financial year, by co-borrower share and tax regime
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

from sqlalchemy.orm import Session

from app.engine import loan as loan_engine
from app.engine import loan_schedule as sched
from app.knowledge import rules
from app.models.schemas import LoanAnchor, Owner

D = Decimal
EXACT = D("2")            # rupees: HDFC's own rounding leaves under ₹1 per instalment


def _letters(db: Session, owner_id: str) -> list[LoanAnchor]:
    return (db.query(LoanAnchor).filter(LoanAnchor.owner_id == owner_id)
            .order_by(LoanAnchor.as_on, LoanAnchor.principal_after.desc()).all())


def _months_between(prev: date, nxt: date) -> list[tuple[int, int]]:
    """The calendar months whose EMI falls after letter `prev` and before letter `nxt` (letters are dated the 1st)."""
    out, (y, m) = [], (prev.year, prev.month)
    while (y, m) < (nxt.year, nxt.month):
        out.append((y, m))
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


def _walk(prev: LoanAnchor, n: int, switch_at: int, new_rate) -> tuple[Decimal, list[Decimal]]:
    """Balance after n instalments at the letter's EMI, the rate changing to `new_rate` from instalment `switch_at`."""
    p, interests = D(prev.principal_after), []
    for i in range(n):
        interest, _repaid, p = sched.step(p, new_rate if i >= switch_at else prev.rate, D(prev.emi))
        interests.append(interest)
    return p, interests


def reconcile(db: Session, owner_id: str) -> list[dict]:
    """One row per interval between consecutive letters: does rolling the earlier letter forward give the later one?"""
    L = _letters(db, owner_id)
    out = []
    for prev, a in zip(L, L[1:]):
        due = _months_between(prev.as_on, a.as_on)
        seen = {(d.year, d.month): (d, amt) for d, amt in loan_engine._emi_debits(db, owner_id, prev.as_on, D(prev.emi), upto=a.as_on)}
        unseen = [m for m in due if m not in seen]
        target = D(a.principal_after) + D(a.prepaid)                      # what the lender owed just before this prepayment
        rate_changed = D(prev.rate) != D(a.rate)
        fits = []
        for k in (range(len(due) + 1) if rate_changed else [len(due)]):
            end, _ = _walk(prev, len(due), k, a.rate)
            fits.append((abs(target - end), target - end, k))
        fits.sort()
        best_abs, best_diff, k = fits[0]
        row = {"from": prev.as_on.isoformat(), "to": a.as_on.isoformat(), "instalments": len(due),
               "unseen_months": [f"{y}-{m:02d}" for y, m in unseen], "rate_from": float(prev.rate), "rate_to": float(a.rate),
               "emi": float(prev.emi), "expected": float(target - best_diff), "letter": float(target),
               "difference": float(best_diff.quantize(D("0.01"))), "rate_change_from_instalment": None, "debits": []}
        if rate_changed and k < len(due) and best_abs <= EXACT:
            first = due[k]
            row["rate_change_from_instalment"] = f"{first[0]}-{first[1]:02d}"
            row["rate_change_date"] = seen[first][0].isoformat() if first in seen else None
        if best_abs <= EXACT:
            row["status"] = "exact_if_unseen_paid" if unseen else "exact"
        else:
            row["status"] = "differs"
            why = []
            if rate_changed:
                why.append("the rate changed in this interval and no single reset month fits")
            if unseen:
                why.append(f"{len(unseen)} instalment(s) have no debit in the data")
            if D(prev.emi) != D(a.emi):
                why.append("the EMI changed between the two letters")
            row["why"] = why
        for m in due:
            if m in seen:
                d, amt = seen[m]
                row["debits"].append({"date": d.isoformat(), "debited": float(amt), "emi_in_letter": float(prev.emi),
                                      "lag": abs(amt - D(prev.emi)) > D("1")})
        out.append(row)
    return out


def repo_pass_through(db: Session, owner_id: str) -> dict:
    """Loan rate minus the repo in force on each letter's date: constant means the loan passes the repo through 1:1."""
    rows = []
    for a in _letters(db, owner_id):
        try:
            repo = rules.value("rbi.repo_rate", a.as_on)
        except rules.RuleNotInForce:
            continue
        rows.append({"as_on": a.as_on.isoformat(), "loan_rate": float(a.rate), "repo": float(repo),
                     "spread": float(D(a.rate) - repo)})
    spreads = {round(r["spread"], 3) for r in rows}
    return {"letters": rows, "spread": next(iter(spreads)) if len(spreads) == 1 else None,
            "constant": len(spreads) == 1 and len(rows) >= 3}


def _next_emi_date(last_debit: date) -> date:
    return sched.add_months(last_debit, 1)


def report(db: Session, owner_id: str, today: date | None = None) -> dict | None:
    today = today or date.today()
    res = loan_engine.outstanding(db, owner_id)
    if res is None:
        return None
    a, L = res["anchor"], _letters(db, owner_id)
    principal, rate, emi = res["principal"], D(a.rate), D(a.emi)
    left_now = a.emis_left - res["emis_applied"] if a.emis_left else None
    nxt = _next_emi_date(res["as_of"])
    now = sched.payoff(principal, rate, emi)

    letters, total_prepaid = [], D(0)
    for x in L:
        calc = sched.payoff(D(x.principal_after), x.rate, D(x.emi))
        saved = sched.interest_saved(D(x.principal_after) + D(x.prepaid), D(x.prepaid), x.rate, D(x.emi))
        total_prepaid += D(x.prepaid)
        letters.append({"as_on": x.as_on.isoformat(), "prepaid": float(x.prepaid), "principal_after": float(x.principal_after),
                        "rate": float(x.rate), "emi": float(x.emi), "emis_left_letter": x.emis_left,
                        "emis_left_calculated": calc["months"] if calc else None,
                        "interest_saved": float(saved["interest_saved"]) if saved else None,
                        "months_saved": saved["months_saved"] if saved else None})
    first = L[0]
    first_end = sched.add_months(date(first.as_on.year, first.as_on.month, 1), first.emis_left - 1) if first.emis_left else None
    now_end = sched.payoff_date(nxt, now["months"]) if now else None

    pt = repo_pass_through(db, owner_id)
    repo_now = rules.value("rbi.repo_rate", today)
    forecast = None
    if pt["constant"]:
        new_rate = repo_now + D(str(pt["spread"]))
        if new_rate != rate and now:
            keep_emi = sched.payoff(principal, new_rate, emi)
            forecast = {"label": "forecast, replaced by the next HDFC letter", "repo_now": float(repo_now),
                        "spread": pt["spread"], "rate_now": float(rate), "rate_expected": float(new_rate),
                        "if_emi_unchanged": keep_emi and {
                            "instalments": keep_emi["months"], "extra_instalments": keep_emi["months"] - now["months"],
                            "ends": sched.payoff_date(nxt, keep_emi["months"]).isoformat(),
                            "extra_interest": float(keep_emi["interest"] - now["interest"])},
                        "if_end_date_unchanged": {"emi": float(sched.emi_for(principal, new_rate, now["months"])),
                                                  "emi_now": float(emi)} if now["months"] else None}
    recs = reconcile(db, owner_id)
    lags = []
    for r in recs:
        if r["rate_change_from_instalment"] and r.get("rate_change_date"):
            for e in rules.load()["rbi.repo_rate"]:
                if e.value + D(str(pt["spread"] or 0)) == D(str(r["rate_to"])) and e.effective_from < date.fromisoformat(r["rate_change_date"]):
                    lags.append({"repo_changed": e.effective_from.isoformat(), "rate_changed_with_instalment": r["rate_change_date"],
                                 "days": (date.fromisoformat(r["rate_change_date"]) - e.effective_from).days})
    return {
        "owner": owner_id, "lender": a.lender, "loan_no": a.loan_no,
        "principal": float(principal), "as_of": res["as_of"].isoformat(), "rate": float(rate), "emi": float(emi),
        "anchor": {"as_on": a.as_on.isoformat(), "principal_after": float(a.principal_after), "emis_left": a.emis_left},
        "emis_left_now": left_now, "next_emi": nxt.isoformat(),
        "payoff": now and {"instalments": now["months"], "ends": now_end.isoformat(), "interest_remaining": float(now["interest"]),
                           "matches_letter": left_now is None or abs(now["months"] - left_now) <= 1},
        "since_first_letter": {"first_letter": first.as_on.isoformat(), "emis_left_then": first.emis_left,
                               "ended_then": first_end and first_end.isoformat(), "ends_now": now_end and now_end.isoformat(),
                               "prepaid_total": float(total_prepaid),
                               "note": "interest saved is valued for each prepayment on its own (that month's rate and EMI); later "
                                       "prepayments overlap earlier ones, so the figures do not add up"},
        "letters": letters, "reconciliation": recs, "repo": pt, "forecast": forecast, "reset_lag_evidence": lags,
    }


def fy_bounds(today: date) -> tuple[date, date]:
    start = date(today.year if today.month >= 4 else today.year - 1, 4, 1)
    return start, date(start.year + 1, 3, 31)


def fy_split(db: Session, owner_id: str, today: date | None = None) -> dict | None:
    """Interest and principal of the current financial year: instalments already paid (from the letters and debits)
    and the remaining months at today's rate and EMI. Prepayments are listed separately."""
    today = today or date.today()
    res = loan_engine.outstanding(db, owner_id)
    if res is None:
        return None
    start, end = fy_bounds(today)
    L = _letters(db, owner_id)
    paid_int = paid_prin = D(0)
    approx = False
    recs = {r["to"]: r for r in reconcile(db, owner_id)}
    for prev, a in zip(L, L[1:]):                                     # intervals between letters: the lender's own balances
        due = _months_between(prev.as_on, a.as_on)
        row = recs[a.as_on.isoformat()]
        approx |= row["status"] == "differs" and a.as_on >= start
        k = row["rate_change_from_instalment"]
        sw = due.index(tuple(int(x) for x in k.split("-"))) if k else len(due)
        p = D(prev.principal_after)
        for i, (y, m) in enumerate(due):
            interest, repaid, p = sched.step(p, a.rate if i >= sw else prev.rate, D(prev.emi))
            if start <= date(y, m, 5) <= end:
                paid_int += interest; paid_prin += repaid
    _p, _last, rows = loan_engine.roll(db, owner_id, res["anchor"])
    for r in rows:
        if start <= r["date"] <= end:
            paid_int += r["interest"]; paid_prin += r["principal"]
    prepaid = sum((D(x.prepaid) for x in L if start <= x.as_on <= end), D(0))
    # the rest of the year: one instalment per month still to come, at today's rate and EMI
    p, rate, emi = res["principal"], D(res["anchor"].rate), D(res["anchor"].emi)
    rest_int = rest_prin = D(0)
    d = _next_emi_date(res["as_of"])
    while d <= end and p > 0:
        interest, repaid, p = sched.step(p, rate, emi)
        rest_int += interest; rest_prin += repaid
        d = sched.add_months(d, 1)
    return {"fy_start": start.isoformat(), "fy_end": end.isoformat(), "approximate": bool(approx),
            "interest_paid": float(paid_int), "principal_paid": float(paid_prin), "prepaid": float(prepaid),
            "interest_year": float(paid_int + rest_int), "principal_year": float(paid_prin + rest_prin)}


def _certificate(db: Session, owner_id: str, fy: str, kind: str):
    from app.models.schemas import LoanTaxCertificate
    return (db.query(LoanTaxCertificate).filter(LoanTaxCertificate.owner_id == owner_id, LoanTaxCertificate.fy == fy,
                                                LoanTaxCertificate.kind == kind)
            .order_by(LoanTaxCertificate.doc_date.desc()).first())


def year_figures(db: Session, owner_id: str, fy: str, today: date) -> dict | None:
    """Interest and principal of one financial year, from the best evidence there is for it:
      HDFC's final certificate (the lender's own figures)  >  the schedule built from the letters (the current year, which
      knows the prepayments)  >  HDFC's provisional statement (issued in advance, ignores prepayments: a fallback, labelled)."""
    start = date(int(fy[:4]), 4, 1)
    L = _letters(db, owner_id)
    if not L:
        return None
    prepaid = sum((D(x.prepaid) for x in L if start <= x.as_on <= date(start.year + 1, 3, 31)), D(0))
    complete = L[0].as_on <= date(start.year, 5, 1)             # the letters reach back to the start of that year
    fin, prov = _certificate(db, owner_id, fy, "final"), _certificate(db, owner_id, fy, "provisional")
    base = {"fy": fy, "prepaid": float(prepaid), "prepaid_complete": complete}
    if fin:
        return {**base, "source": "HDFC final certificate", "interest": float(fin.interest + (fin.pre_emi_interest or 0)),
                "principal": float(fin.principal), "exact": True}
    if tg_fy(today) == fy:
        sp = fy_split(db, owner_id, today)
        if sp:
            return {**base, "source": "schedule built from HDFC's letters (estimate for the full year)", "interest": sp["interest_year"],
                    "principal": sp["principal_year"], "exact": False,
                    "provisional_statement": prov and {"interest": float(prov.interest), "principal": float(prov.principal)}}
    if prov:
        return {**base, "source": "HDFC provisional statement (before prepayments; the final certificate replaces it)",
                "interest": float(prov.interest), "principal": float(prov.principal), "exact": False}
    return None


def tg_fy(d: date) -> str:
    y = d.year if d.month >= 4 else d.year - 1
    return f"{y}-{str(y + 1)[2:]}"


def _claims(db: Session, fig: dict, today: date) -> list[dict]:
    fy_end = date(int(fig["fy"][:4]) + 1, 3, 31)
    cap24, cap80c = rules.value("income_tax.24b_cap", fy_end), rules.value("income_tax.80c_cap", fy_end)
    people = []
    for o in db.query(Owner).filter(Owner.id != "family", ~Owner.id.like(".%")).order_by(Owner.id).all():
        share = D(str(o.home_loan_share_pct or 0))
        if share <= 0:
            continue
        regime = (o.tax_regime or "old").lower()
        mine_int, mine_prin = D(str(fig["interest"])) * share / 100, D(str(fig["principal"])) * share / 100
        mine_pre = D(str(fig["prepaid"])) * share / 100
        row = {"owner": o.id, "share_pct": float(share), "regime": regime, "interest_share": float(mine_int),
               "principal_share": float(mine_prin), "prepaid_share": float(mine_pre)}
        if regime == "old":
            row.update(claim_24b=float(min(mine_int, cap24)), cap_24b=float(cap24), interest_above_cap=float(max(mine_int - cap24, 0)),
                       principal_for_80c=float(mine_prin + mine_pre), cap_80c=float(cap80c),
                       uses_whole_80c=bool(mine_prin + mine_pre >= cap80c))
        else:
            row.update(claim_24b=0.0, note="new regime: no deduction for a self-occupied home loan")
        people.append(row)
    return people


def tax(db: Session, owner_id: str, today: date | None = None) -> dict | None:
    """What each co-borrower can claim: s.24(b) interest (cap per owner, on their share) and s.80C principal, old regime only.
    `fy` is the current year from the schedule; `years` lists recent years with the best evidence for each (HDFC's certificates)."""
    today = today or date.today()
    fy = fy_split(db, owner_id, today)
    if fy is None:
        return None
    cap24, cap80c = rules.value("income_tax.24b_cap", today), rules.value("income_tax.80c_cap", today)
    people = []
    for o in db.query(Owner).filter(Owner.id != "family", ~Owner.id.like(".%")).order_by(Owner.id).all():
        share = D(str(o.home_loan_share_pct or 0))
        if share <= 0:
            continue
        regime = (o.tax_regime or "old").lower()
        mine_int, mine_prin = D(str(fy["interest_year"])) * share / 100, D(str(fy["principal_year"])) * share / 100
        mine_pre = D(str(fy["prepaid"])) * share / 100
        row = {"owner": o.id, "share_pct": float(share), "regime": regime, "interest_share": float(mine_int),
               "principal_share": float(mine_prin), "prepaid_share": float(mine_pre)}
        if regime == "old":
            row.update(claim_24b=float(min(mine_int, cap24)), cap_24b=float(cap24), interest_above_cap=float(max(mine_int - cap24, 0)),
                       principal_for_80c=float(mine_prin + mine_pre), cap_80c=float(cap80c),
                       uses_whole_80c=bool(mine_prin + mine_pre >= cap80c))
        else:
            row.update(claim_24b=0.0, note="new regime: no deduction for a self-occupied home loan")
        people.append(row)
    years = []
    cur = int(tg_fy(today)[:4])
    for y in range(cur, cur - 3, -1):
        fyl = f"{y}-{str(y + 1)[2:]}"
        fig = year_figures(db, owner_id, fyl, today)
        if fig:
            years.append({**fig, "owners": _claims(db, fig, today)})
    return {"fy": fy, "owners": people, "years": years,
            "note": "Interest is split by the recorded share of the loan. Principal for 80C counts EMI principal plus part-prepayments, "
                    "which HDFC's own certificate says qualify (the 1.5 L cap is shared with PPF, EPF and the rest). Completed years use "
                    "HDFC's final certificate."}
