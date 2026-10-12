"""
Cross-page consistency checks.

Each check states something that must be true no matter which page, card or tile you read it from.
`fail` = an invariant is broken (a bug or stale derived data); `warn` = the data itself has a gap
(a missing statement, unreviewed payees). Runs after every sync and on demand at
/api/health/consistency, so a disagreement surfaces the day it appears instead of months later.
"""
from __future__ import annotations

import logging
from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal
from typing import Callable

from sqlalchemy import func, text
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)
TOL = 1.0          # ₹1 — everything below is rounding


def _check(name: str, ok: bool, detail: str, severity: str = "fail", **values) -> dict:
    return {"name": name, "ok": bool(ok), "severity": "ok" if ok else severity, "detail": detail, "values": values}


def _owners(db: Session) -> list[str]:
    from app.engine.networth import real_owners
    return [o.id for o in real_owners(db)]


def _latest_nw(db: Session) -> dict[str, object]:
    from app.models.schemas import NetWorthSnapshot
    sub = (db.query(NetWorthSnapshot.owner_id, func.max(NetWorthSnapshot.snapshot_date).label("d"))
           .group_by(NetWorthSnapshot.owner_id).subquery())
    rows = (db.query(NetWorthSnapshot)
            .join(sub, (NetWorthSnapshot.owner_id == sub.c.owner_id) & (NetWorthSnapshot.snapshot_date == sub.c.d)).all())
    return {r.owner_id: r for r in rows}


def family_equals_members(db: Session) -> dict:
    nw = _latest_nw(db)
    owners = _owners(db)
    fam = nw.get("family")
    if not fam or not owners:
        return _check("family_equals_self_plus_spouse", False, "no net-worth snapshot yet", "warn")
    bad = {}
    for f in ("bank_balance", "equity_value", "mf_value", "ppf_value", "fd_value", "real_estate_value",
              "other_assets", "total_assets", "total_liabilities", "net_worth"):
        s = sum(float(getattr(nw[o], f) or 0) for o in owners if o in nw)
        if abs(s - float(getattr(fam, f) or 0)) > TOL:
            bad[f] = {"family": float(getattr(fam, f) or 0), "members": s}
    return _check("family_equals_self_plus_spouse", not bad,
                  "Family net worth equals the sum of its members" if not bad else
                  f"Family differs from the members' sum in {', '.join(bad)}", differences=bad)


def networth_matches_holdings(db: Session) -> dict:
    from app.engine.holdings_view import latest_holdings
    nw, bad = _latest_nw(db), {}
    for o in _owners(db):
        if o not in nw:
            continue
        eq = mf = 0.0
        for h in latest_holdings(db, owner_ids=[o]):
            v = float(h.current_value or 0)
            if h.holding_type == "mutual_fund":
                mf += v
            else:
                eq += v
        if abs(eq - float(nw[o].equity_value or 0)) > TOL or abs(mf - float(nw[o].mf_value or 0)) > TOL:
            bad[o] = {"holdings": {"equity": eq, "mf": mf},
                      "net_worth": {"equity": float(nw[o].equity_value or 0), "mf": float(nw[o].mf_value or 0)}}
    return _check("networth_matches_holdings", not bad,
                  "Net-worth equity and funds equal the Investments table" if not bad else
                  "Net worth is stale versus holdings (re-run snapshot)", differences=bad)


def returns_match_holdings(db: Session) -> dict:
    from app.engine.holdings_view import latest_holdings
    from app.engine.portfolio_xirr import compute_owner_returns
    bad = {}
    for o in _owners(db):
        ret = {c["class"]: float(c["current_all"]) for c in compute_owner_returns(db, o)["by_class"]}
        held = defaultdict(float)
        for h in latest_holdings(db, owner_ids=[o]):
            held[h.holding_type or "equity"] += float(h.current_value or 0)
        for cls in ("equity", "mutual_fund", "bonds"):
            if abs(ret.get(cls, 0.0) - held.get(cls, 0.0)) > TOL:
                bad.setdefault(o, {})[cls] = {"returns": ret.get(cls, 0.0), "holdings": held.get(cls, 0.0)}
    nw = _latest_nw(db)
    for o in _owners(db):                                   # deposits: returns page value == net-worth value
        by = {c["class"]: float(c["current_all"]) for c in compute_owner_returns(db, o)["by_class"]}
        for cls, field in (("fd", "fd_value"), ("ppf", "ppf_value")):
            if o in nw and abs(by.get(cls, 0.0) - float(getattr(nw[o], field) or 0)) > TOL:
                bad.setdefault(o, {})[cls] = {"returns": by.get(cls, 0.0), "net_worth": float(getattr(nw[o], field) or 0)}
    return _check("returns_match_holdings", not bad,
                  "Returns page values equal the Investments table" if not bad else
                  "Returns page values differ from the Investments table", differences=bad)


def goals_are_one_story(db: Session) -> dict:
    from app.engine.goals import project_all_goals
    from app.models.schemas import Goal
    bad = {}
    for p in project_all_goals(db):
        g = db.get(Goal, p.goal_id)
        if g is not None and abs(float(g.monthly_sip_needed or 0) - p.monthly_sip_needed) > TOL:
            bad[p.name] = {"stored": float(g.monthly_sip_needed or 0), "projection": p.monthly_sip_needed}
    return _check("goal_sip_stored_equals_projection", not bad,
                  "Goal cards, projections table and Grafana use the same SIP figure" if not bad else
                  "Stored goal SIP differs from the live projection (run goal refresh)", differences=bad)


def goal_corpus_matches_holdings(db: Session) -> dict:
    from app.engine.goal_returns import goal_tags
    from app.engine.holdings_view import latest_holdings
    from app.models.schemas import Goal
    by_tag = defaultdict(float)
    for h in latest_holdings(db, owner_ids=_owners(db)):
        if h.goal_tag:
            by_tag[h.goal_tag] += float(h.current_value or 0)
    bad = {}
    for g in db.query(Goal).filter(Goal.goal_type.in_(["child_education", "child_marriage"])).all():
        want = sum(by_tag.get(t, 0.0) for t in goal_tags(g))
        if want > 0 and abs(want - float(g.current_corpus or 0)) > 1.0:
            bad[g.name] = {"corpus": float(g.current_corpus or 0), "tagged_holdings": want}
    return _check("goal_corpus_matches_tagged_holdings", not bad,
                  "Goal corpus equals the tagged holdings" if not bad else
                  "Goal corpus is stale versus tagged holdings", differences=bad)


def expense_definitions_agree(db: Session) -> dict:
    """Overview chart, Transactions summary, breakdown donut and Grafana read one definition."""
    start = (date.today().replace(day=1) - timedelta(days=90)).replace(day=1)   # whole months only
    view = float(db.execute(text("SELECT COALESCE(SUM(amount),0) FROM v_cashflow WHERE flow='expense' AND date >= :s"),
                            {"s": start}).scalar() or 0)
    monthly = float(db.execute(text("SELECT COALESCE(SUM(expense),0) FROM v_cashflow_monthly WHERE month >= :s"),
                               {"s": start.replace(day=1)}).scalar() or 0)
    rows = float(db.execute(text("SELECT COALESCE(SUM(amount),0) FROM v_cashflow WHERE flow='expense' AND date >= :s "
                                 "AND category IS NOT NULL"), {"s": start}).scalar() or 0)
    ok = abs(view - monthly) <= TOL and abs(view - rows) <= TOL
    return _check("expense_definition_is_shared", ok, "Every expense figure comes from v_cashflow" if ok
                  else "Expense totals differ between the view and its monthly roll-up",
                  view=view, monthly=monthly)


def no_duplicate_bank_rows(db: Session) -> dict:
    from app.txn.dedupe import dedupe_bank_transactions
    r = dedupe_bank_transactions(db, apply=False)
    return _check("no_duplicate_bank_rows", r["duplicates"] == 0,
                  "No bank transaction appears twice" if r["duplicates"] == 0 else
                  f"{r['duplicates']} duplicate bank rows (₹{r['amount']:,.0f}) — run maintenance", **r)


def no_unpaired_own_transfers(db: Session) -> dict:
    from app.txn.transfers import pair_own_transfers
    r = pair_own_transfers(db, apply=False)
    return _check("own_transfers_are_not_spending", r["pairs"] == 0,
                  "Money moved between own accounts is never counted as spending or income" if r["pairs"] == 0 else
                  f"{r['pairs']} own-account transfer pairs are still tagged as spending/income", **r)


def trades_are_investments(db: Session) -> dict:
    from app.models.schemas import Account, Transaction
    n = (db.query(func.count(Transaction.id)).join(Account, Transaction.account_id == Account.id)
         .filter(Account.institution.in_(["Zerodha", "Zerodha Coin"]),
                 Transaction.category.notin_(["investment", "transfer"])).scalar()) or 0
    return _check("trades_are_tagged_investment", n == 0,
                  "Every broker cashflow is an investment or a non-cash unit count" if n == 0 else
                  f"{n} broker rows carry a spending/income category", rows=n)


def no_phantom_owners(db: Session) -> dict:
    from app.models.schemas import ManualAsset, Owner
    n = (db.query(func.count(ManualAsset.id)).join(Owner, Owner.id == ManualAsset.owner_id)
         .filter(Owner.id.like(".%")).scalar()) or 0
    return _check("no_assets_on_archived_owners", n == 0,
                  "No assets sit on archived placeholder owners" if n == 0 else
                  f"{n} assets belong to an archived owner and inflate Family", assets=n)


def ledger_reconciliation(db: Session) -> dict:
    from app.models.schemas import ReconIssue
    errs = (db.query(func.count(ReconIssue.id)).filter(ReconIssue.status == "open", ReconIssue.severity == "error").scalar()) or 0
    return _check("ledger_reconciliation", errs == 0,
                  "Every change in holdings is explained by a trade" if errs == 0 else
                  f"{errs} unexplained holding changes — see /api/ledger/issues", "warn", errors=errs)


def data_freshness(db: Session) -> dict:
    from app.models.schemas import Account, Balance
    stale = []
    for a in db.query(Account).filter(Account.account_type == "bank", Account.is_active.is_(True)).all():
        last = db.query(func.max(Balance.snapshot_date)).filter(Balance.account_id == a.id).scalar()
        if last and (date.today() - last).days > 45:
            stale.append({"account": f"{a.institution} {a.account_ref}", "owner": a.owner_id, "last_balance": str(last)})
    return _check("bank_balances_are_recent", not stale,
                  "Every bank balance is under 45 days old" if not stale else
                  f"{len(stale)} bank accounts have no statement in 45+ days (net-worth bank balance includes them)",
                  "warn", accounts=stale)


def unreviewed_payees(db: Session) -> dict:
    from app.models.schemas import Transaction
    n = (db.query(func.count(Transaction.id)).filter(Transaction.needs_review.is_(True)).scalar()) or 0
    return _check("payees_awaiting_review", n == 0,
                  "Every payee is labelled" if n == 0 else f"{n} transactions wait in the Review list",
                  "warn", rows=n)


def alerts_match_statements(db: Session) -> dict:
    """An alert inside a statement's date range with no matching row = the statement parse lost a transaction."""
    from app.txn.alert_recon import reconcile_alerts
    r = reconcile_alerts(db)
    return _check("alerts_have_statement_rows", r["unmatched"] == 0,
                  f"Every bank alert inside a statement period has its statement row "
                  f"({r['matched']} matched, {r['pending']} awaiting statements)" if r["unmatched"] == 0 else
                  f"{r['unmatched']} bank alerts have no row in a statement that covers their date — see /api/health/alerts",
                  "warn", **r)


def product_invariants(db: Session) -> dict:
    """The cross-page rules in app/invariants.py (docs/INVARIANTS.md): a page must not drop or disagree."""
    from app.invariants import INVARIANTS
    broken = {}
    for name, fn in INVARIANTS:
        found = fn(db)
        if found:
            broken[name] = found[:5]
    return _check("product_invariants", not broken,
                  f"All {len(INVARIANTS)} cross-page invariants hold" if not broken else
                  "Broken: " + "; ".join(f"{k} ({v[0]})" for k, v in broken.items()), "fail", broken=broken)


def ppf_reconciles(db: Session) -> dict:
    """PPF: each measured year must close (opening + deposits + interest by the rule = closing) with every deposit
    on a bank statement; a deposit found only through its interest is reported, an uncertain one is a warning."""
    from app.api.routes.health import get_ppf_reconciliation
    rows = get_ppf_reconciliation(db)
    unsure = [(r["owner"], y["year"], y["inferred"]) for r in rows for y in r["years"]
              if y["inferred"] and y["inferred"]["confidence"] != "high"]
    lower = [(r["owner"], y["year"]) for r in rows for y in r["years"] if y["status"].startswith("balance lower")]
    unmeasured = [r["owner"] for r in rows if not r["measured"]]
    ok = not unsure and not lower and not unmeasured
    parts = []
    if unsure:
        parts.append(f"{len(unsure)} PPF year(s) have a deposit whose month cannot be pinned down")
    if lower:
        parts.append(f"{len(lower)} PPF year(s) close lower than deposits + interest explain (withdrawal?)")
    if unmeasured:
        parts.append(f"PPF of {', '.join(sorted(set(unmeasured)))} has no two consecutive 31-March statements")
    return _check("ppf_years_reconcile", ok, "Every measured PPF year reconciles: opening + deposits + interest = closing"
                  if ok else "; ".join(parts) + " — see /api/health/ppf", "warn", unsure=unsure, lower=lower)


def deposits_match_alerts(db: Session) -> dict:
    """SBI deposit accounts have no statements; their alerts are the only live balance. They must agree with Manual Assets."""
    from app.txn.alert_recon import alert_summary
    a = alert_summary(db, recent=1)
    drift = [d for d in a["deposit_accounts"] if abs(d["difference"]) > 1.0]
    miss = a["accounts_missing_from_assets"]
    ok = not drift and not miss
    parts = []
    if drift:
        parts.append(f"{len(drift)} deposit balances differ from Manual Assets (net ₹{sum(d['difference'] for d in drift):,.0f})")
    if miss:
        parts.append(f"{len(miss)} accounts hold money (₹{sum(m['alert_balance'] for m in miss):,.0f}) but are not in Manual Assets")
    return _check("deposit_accounts_match_alerts", ok, "Every deposit account seen in alerts is in Manual Assets at the same balance"
                  if ok else "; ".join(parts) + " — see /api/health/alerts", "warn", drift=drift, missing=miss)


def rules_are_current(db: Session) -> dict:
    """The public-rules registry (app/knowledge/rules.yml) is read by hand: warn when a rule in force is over
    120 days old or predates this quarter, so a changed rate/threshold is not silently relied on."""
    from app.knowledge import rules
    old = rules.stale()
    return _check("public_rules_are_current", not old,
                  "Every public rule was verified this quarter" if not old else
                  f"{len(old)} rule(s) need re-reading at their source ({', '.join(r['id'] for r in old[:4])}"
                  f"{'…' if len(old) > 4 else ''}) — see docs/INSTRUMENTS.md", "warn", stale=old)


CHECKS: list[Callable[[Session], dict]] = [
    family_equals_members, networth_matches_holdings, returns_match_holdings, goals_are_one_story,
    goal_corpus_matches_holdings, expense_definitions_agree, no_duplicate_bank_rows,
    no_unpaired_own_transfers, trades_are_investments, no_phantom_owners, ledger_reconciliation,
    data_freshness, unreviewed_payees, alerts_match_statements,
    deposits_match_alerts, ppf_reconciles, rules_are_current, product_invariants,
]


def run(db: Session) -> dict:
    out = []
    for fn in CHECKS:
        try:
            out.append(fn(db))
        except Exception as exc:                          # a broken check must not hide the others
            logger.error("consistency check %s crashed: %s", fn.__name__, exc, exc_info=True)
            db.rollback()
            out.append(_check(fn.__name__, False, f"check crashed: {exc}", "fail"))
    fails = [c for c in out if c["severity"] == "fail"]
    warns = [c for c in out if c["severity"] == "warn"]
    return {"ok": not fails, "failing": len(fails), "warnings": len(warns), "checked": len(out),
            "as_of": date.today().isoformat(), "checks": out}
