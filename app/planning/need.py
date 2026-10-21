"""Spending and the retirement living cost, derived from the transactions (never typed)."""
from __future__ import annotations

from datetime import date

from sqlalchemy import text
from sqlalchemy.orm import Session

# Not part of the cost of living once retired (docs/PLANNING.md §3): the loan ends, the term premium ends, children are funded by their
# own goals, family support is parents' care covered by the health goal. Everything else (incl. the large unlabelled "other") stays in.
RETIREMENT_EXCLUDED = {("emi", "emi_regular"), ("insurance", "term_insurance"), ("child_expenses", None), ("family_support", None)}


def month_window(today: date | None = None, months: int = 12) -> tuple[date, date]:
    """The last `months` complete months: [start, end)."""
    today = today or date.today()
    end = today.replace(day=1)
    y, m = end.year, end.month - months
    while m <= 0:
        y, m = y - 1, m + 12
    return date(y, m, 1), end


def split_spending(rows: list[tuple[str | None, str | None, float]]) -> dict:
    """rows: (category, subcategory, amount) over the window → total, by category, and the retirement-relevant core."""
    by: dict[tuple[str, str], float] = {}
    for cat, sub, amt in rows:
        by[(cat or "uncategorised", sub or "")] = by.get((cat or "uncategorised", sub or ""), 0.0) + float(amt or 0)
    total = sum(by.values())
    excluded = {k: v for k, v in by.items() if (k[0], k[1]) in RETIREMENT_EXCLUDED or (k[0], None) in RETIREMENT_EXCLUDED}
    core = total - sum(excluded.values())
    return {
        "total": total, "retirement_core": core,
        "excluded": [{"category": c, "subcategory": s, "amount": v} for (c, s), v in sorted(excluded.items(), key=lambda kv: -kv[1])],
        "by_category": [{"category": c, "subcategory": s, "amount": v} for (c, s), v in sorted(by.items(), key=lambda kv: -kv[1])][:15],
    }


def household_spending(db: Session, today: date | None = None, months: int = 12) -> dict:
    start, end = month_window(today, months)
    rows = db.execute(text(
        "SELECT category, subcategory, SUM(amount) FROM v_cashflow WHERE flow = 'expense' AND NOT provisional "
        "AND month >= :s AND month < :e AND owner_id IN ('user', 'wife') GROUP BY 1, 2"), {"s": start, "e": end}).all()
    out = split_spending([(r[0], r[1], float(r[2])) for r in rows])
    inc = db.execute(text("SELECT COALESCE(SUM(amount), 0) FROM v_cashflow WHERE flow = 'income' AND month >= :s AND month < :e "
                          "AND owner_id IN ('user', 'wife')"), {"s": start, "e": end}).scalar()
    out.update({"window": [start.isoformat(), end.isoformat()], "months": months, "income": float(inc or 0)})
    return out
