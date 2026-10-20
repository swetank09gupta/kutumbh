"""
Does each bank account's running balance chain from day to day?

Every statement row prints the balance after it. Taking the account day by day, yesterday's closing balance plus today's credits
minus today's debits must equal one of today's printed balances (the last row of the day; the order inside a day is not kept).
When it does not, the amount of the difference is what is missing (positive: debits we do not have / credits we have twice, the balance is lower than our rows explain; negative: credits we
do not have) at that date. It needs no alerts and no particular bank: HDFC, SBI, Axis, Union all print balances.

A break is a pointer, never a correction: it says which day to re-read and by how much, so a lost row, a row merged with its
twin, or a missing statement month shows up the day it happens instead of as a wrong total much later.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import text
from sqlalchemy.orm import Session

TOLERANCE = Decimal("0.01")


def day_breaks(rows: list[tuple[date, str, Decimal, Decimal]]) -> list[dict]:
    """rows: (date, 'debit'|'credit', amount, balance_after) of ONE account. Returns [{date, missing}] (see module doc).

    After a break the day's closing balance is ambiguous (the rows of a day have no order), so every printed balance of that
    day stays a candidate until the next day's arithmetic picks the one that chains; the break then carries that candidate's
    difference."""
    days: dict[date, list[tuple[str, Decimal, Decimal]]] = defaultdict(list)
    for d, kind, amount, balance in rows:
        if balance is not None:
            days[d].append((kind, Decimal(amount), Decimal(balance)))
    out: list[dict] = []
    prevs: list[Decimal] | None = None                  # possible closing balances of the previous day
    pending: dict | None = None                         # the open break: {"i": index in out, "diff": {candidate: difference}}
    for d in sorted(days):
        net = sum((a if k == "credit" else -a) for k, a, _ in days[d])
        candidates = [b for _, _, b in days[d]]
        if prevs is None:
            prevs = [candidates[-1]]
            continue
        hit = next((c for p in prevs for c in candidates if abs(c - (p + net)) < TOLERANCE), None)
        resolved_by = next((p for p in prevs for c in candidates if abs(c - (p + net)) < TOLERANCE), None)
        if hit is not None:
            if pending is not None and resolved_by is not None:
                out[pending["i"]]["missing"] = pending["diff"][resolved_by]
                pending = None
            prevs = [hit]
            continue
        if pending is not None:                          # the next day did not settle it: keep the nearest reading
            pending = None
        want = prevs[0] + net
        pending = {"i": len(out), "diff": {c: want - c for c in candidates}}
        out.append({"date": d, "missing": min(pending["diff"].values(), key=abs)})
        prevs = candidates
    return out


def gap_month(db: Session, account_id, day: date) -> str | None:
    """'YYYY-MM' when a break in the first days of a month follows a month for which the account has neither a row nor a
    statement balance: the statement for that month never arrived, so the break is the month's net movement (not a lost row)."""
    if day.day > 10:
        return None
    prev = (day.replace(day=1) - timedelta(days=1)).replace(day=1)
    seen = db.execute(text(
        "SELECT (SELECT count(*) FROM transactions WHERE account_id = :a AND date_trunc('month', date) = :m)"
        " + (SELECT count(*) FROM balances WHERE account_id = :a AND date_trunc('month', snapshot_date) = :m)"),
        {"a": str(account_id), "m": prev}).scalar()
    return None if seen else prev.strftime("%Y-%m")


def alert_evidence(db: Session, account_id, month: str) -> dict:
    """What the bank's own alert mails say happened in a month whose statement never arrived: {count, net_debit} (debits minus
    credits, copies of the same alert excluded). Evidence only: no row is created from it (ADR-0025)."""
    from app.models.schemas import Account
    from app.txn.alert_recon import account_alerts
    acc = db.get(Account, account_id)
    rows = [a for a in account_alerts(db, acc) if a.status != "dup" and a.txn_date.strftime("%Y-%m") == month]
    net = sum((Decimal(a.amount) if a.direction == "debit" else -Decimal(a.amount)) for a in rows)
    return {"count": len(rows), "net_debit": net}


def account_breaks(db: Session) -> list[dict]:
    """[{account, owner, days, breaks:[{date, missing, gap?}]}] for every bank account that prints balances.
    `gap` names the month whose statement never arrived (see gap_month); `alerts` is what that month's alert mails add up to and
    `explained` says they account for the break to the paisa."""
    out = []
    for aid, inst, owner, ref in db.execute(text(
            "SELECT id, institution, owner_id, account_ref FROM accounts "
            "WHERE account_type = 'bank' AND is_active AND owner_id NOT LIKE '.%' ORDER BY institution, owner_id")).all():
        rows = db.execute(text("SELECT date, transaction_type, amount, balance_after FROM transactions "
                               "WHERE account_id = :a AND balance_after IS NOT NULL"), {"a": aid}).all()
        if not rows:
            continue
        breaks = day_breaks(rows)
        for b in breaks:
            if (g := gap_month(db, aid, b["date"])):
                b["gap"] = g
                ev = alert_evidence(db, aid, g)
                b["alerts"] = ev
                b["explained"] = ev["count"] > 0 and abs(ev["net_debit"] - Decimal(b["missing"])) < TOLERANCE
        out.append({"account": f"{inst} {ref}", "owner": owner, "rows": len(rows), "breaks": breaks})
    return out
