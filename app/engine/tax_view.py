"""
The tax lens: what each person earned and realised per financial year, and what is on the table, from data the system
already proves. Rates, thresholds and caps come from the rules registry; nothing here is typed in.

    gains       capital gains from the ledger's trades (app.engine.tax_gains), per FY, FIFO
    income      savings and deposit interest (deposit interest grossed up by the TDS the bank deducted), dividends, SGB coupons
    tds         deposit interest per bank against the s.194A threshold; dividend per company against the s.194 threshold
    unrealised  the tax if everything held were sold today, with the LTCG exemption left this year
    issues      data that looks wrong for tax purposes (reported, not corrected)

Slab-rate income is shown as amounts: the slab depends on total income, which is not in the system. The deduction under
80TTA exists only for a person on the old regime (taken from the owner's profile).
"""
from __future__ import annotations

import re
from collections import defaultdict
from datetime import date
from decimal import Decimal
from typing import Optional

from sqlalchemy.orm import Session

from app.engine import tax_gains as tg
from app.knowledge import rules
from app.models.schemas import Account, Owner, TdsDeduction, Transaction

D = Decimal
_DEPOSIT_INT = re.compile(r"INT TRF FRM \d+ TO \d+|INT ON TERMDEP", re.I)
_SAVINGS_INT = re.compile(r"^INTEREST CREDIT|INTEREST ?PAID ?TILL|CREDIT INTEREST|\bINT\.?PD", re.I)
FYS_SHOWN = 4


def _fy_list(today: date) -> list[str]:
    cur = int(tg.fy_of(today)[:4])
    return [f"{y}-{str(y + 1)[2:]}" for y in range(cur, cur - FYS_SHOWN, -1)]


def _threshold(rule: str, fy: str) -> Optional[Decimal]:
    try:
        return rules.value(rule, tg.fy_end(fy))
    except rules.RuleNotInForce:
        return None                                            # not verified for that year: no check is made


def income(db: Session, owner: Owner, today: date) -> list[dict]:
    from app.ledger import dividends
    divs = dividends.scan(db, today)
    rows = (db.query(Transaction, Account.institution).join(Account, Account.id == Transaction.account_id)
            .filter(Account.owner_id == owner.id, Account.account_type == "bank", Transaction.transaction_type == "credit").all())
    tds = defaultdict(lambda: D(0))
    for r in db.query(TdsDeduction).filter(TdsDeduction.owner_id == owner.id):
        tds[tg.fy_of(r.deducted_on)] += D(r.amount)
    regime = (owner.tax_regime or "old").lower()
    out = []
    for fy in _fy_list(today):
        sav = dep = coup = div = D(0)
        by_payer: dict[str, Decimal] = defaultdict(lambda: D(0))
        div_by_company: dict[str, Decimal] = defaultdict(lambda: D(0))
        for t, inst in rows:
            if tg.fy_of(t.date) != fy:
                continue
            narr, amt = t.raw_description or t.description or "", D(t.amount)
            if t.subcategory == "sgb_coupon":
                coup += amt
            elif t.subcategory == "dividend":
                continue                                            # counted from the proven matches below, tagged or not
            elif _DEPOSIT_INT.search(narr):
                dep += amt
                by_payer[inst] += amt
            elif _SAVINGS_INT.search(narr):
                sav += amt
        for m in divs["matched"]:
            if m["owner"] == owner.id and tg.fy_of(date.fromisoformat(m["date"])) == fy:
                div_by_company[m["symbol"]] += D(str(m["amount"]))
                div += D(str(m["amount"]))
        loose = [x for x in divs["unmatched"] + divs["ambiguous"] if x["owner"] == owner.id and tg.fy_of(date.fromisoformat(x["date"])) == fy]
        loose_total = sum((D(str(x["amount"])) for x in loose), D(0))
        tds_fy = tds.get(fy, D(0))
        thr = _threshold("tds.194a.threshold", fy)
        cap80tta = _threshold("income_tax.80tta_cap", fy)
        payers = []
        for inst, net in sorted(by_payer.items()):
            gross = net + (tds_fy if inst == "SBI" else D(0))      # TDS notices come from SBI: the interest paid was net of it
            payers.append({"payer": inst, "interest_credited": float(net), "tds_deducted": float(tds_fy if inst == "SBI" else 0),
                           "gross_interest": float(gross), "threshold": float(thr) if thr else None,
                           "over_threshold": bool(thr and (gross > thr or (inst == "SBI" and tds_fy > 0))),
                           "crossed_by_tds": bool(thr and gross <= thr and inst == "SBI" and tds_fy > 0),
                           "headroom": float(thr - gross) if thr else None})
        dthr = _threshold("tds.194.dividend_threshold", fy)
        out.append({
            "fy": fy, "regime": regime,
            "savings_interest": float(sav), "deposit_interest_credited": float(dep), "tds_on_deposit_interest": float(tds_fy),
            "deposit_interest_gross": float(dep + tds_fy),
            "dividends_proven": float(div), "dividends_unmatched": float(loose_total), "dividends_unmatched_count": len(loose),
            "sgb_coupons": float(coup),
            "investment_income_at_slab": float(sav + dep + tds_fy + div + loose_total + coup),
            "deduction_80tta": (float(min(sav, cap80tta)) if cap80tta is not None else None) if regime == "old" else 0.0,
            "tds_by_payer": payers,
            "dividend_by_company": [{"company": c, "amount": float(a), "threshold": float(dthr) if dthr else None,
                                     "over_threshold": bool(dthr and a > dthr)} for c, a in sorted(div_by_company.items())],
        })
    return out


def _unit_prices(db: Session, owner_id: str) -> dict[str, Decimal]:
    from app.engine.holdings_view import latest_holdings
    return {h.isin: D(str(h.current_value)) / D(str(h.units)) for h in latest_holdings(db, owner_ids=[owner_id])
            if h.isin and h.units and D(str(h.units)) > 0 and h.current_value}


def _issues(db: Session, owners: list[str]) -> list[dict]:
    """Rows the categoriser calls interest/dividend income that do not look like either. A credit would inflate income; a
    debit is counted as an EXPENSE in the cashflow. They are named, not corrected: only you know what they are."""
    credits, debits = [], []
    q = (db.query(Transaction, Account.owner_id, Account.institution).join(Account, Account.id == Transaction.account_id)
         .filter(Account.owner_id.in_(owners), Transaction.category == "income", Transaction.subcategory == "interest_dividend"))
    for t, owner, inst in q:
        narr = t.raw_description or ""
        if D(t.amount) >= 10000 and not (_DEPOSIT_INT.search(narr) or _SAVINGS_INT.search(narr)):
            row = {"owner": owner, "date": t.date.isoformat(), "amount": float(t.amount), "bank": inst, "narration": narr[:50]}
            (credits if t.transaction_type == "credit" else debits).append(row)
    out = []
    if debits:
        out.append({"kind": "debit_labelled_interest_income",
                    "detail": f"{len(debits)} debit(s) of ₹10,000 or more (₹{sum(x['amount'] for x in debits):,.0f} in all) are labelled interest "
                              "income because the bank's statement text mentions interest, so the cashflow counts them as expenses. They look like "
                              "standing-instruction transfers to the owner's own deposits; label them as transfers once confirmed.",
                    "rows": debits[:8]})
    if credits:
        out.append({"kind": "credit_labelled_interest_income",
                    "detail": f"{len(credits)} credit(s) of ₹10,000 or more (₹{sum(x['amount'] for x in credits):,.0f}) are labelled interest income "
                              "but are not interest credits; they are left out of the interest figures here.", "rows": credits[:8]})
    return out


def build(db: Session, today: date | None = None) -> dict:
    from app.engine.amfi_isin import fund_category
    from app.engine.networth import real_owners
    from app.ledger.engine import compute_owner
    today = today or date.today()
    owners = real_owners(db)
    result = {"as_of": today.isoformat(), "fy": tg.fy_of(today), "owners": {}, "issues": _issues(db, [o.id for o in owners])}
    for o in owners:
        led = compute_owner(db, o.id)
        names = {i: (m.get("name") or m.get("symbol") or i) for i, m in led.meta.items()}
        cats = {i: fund_category(i) for i in names if i.startswith("INF")}
        res = tg.fifo(led.trades, names, cats)
        fys = sorted({tg.fy_of(s.day) for s in res.sales} | {tg.fy_of(today)}, reverse=True)
        gains = [tg.year_summary(res.sales, fy) for fy in fys]
        cur = next(g for g in gains if g["fy"] == tg.fy_of(today))
        left = D(str(cur["equity"]["ltcg_exemption"])) - D(str(cur["equity"]["ltcg_exemption_used"]))
        uncov = [u for u in res.unknown_cost]
        result["owners"][o.id] = {
            "regime": (o.tax_regime or "old").lower(),
            "gains": gains,
            "income": income(db, o, today),
            "unrealised": tg.unrealised(res.open_lots, _unit_prices(db, o.id), names, cats, today, left),
            "sold_without_known_cost": uncov[:10], "sold_without_known_cost_count": len(uncov),
        }
    result["notes"] = ("Trade prices are gross: brokerage and stamp duty (allowed) are not netted, STT (not allowed) is not included; "
                       "corporate actions are not applied. Slab-rate income is shown as amounts because the slab depends on total income. "
                       "SGB coupons are taxable income; SGB redemption at maturity is exempt for an original subscriber.")
    return result
