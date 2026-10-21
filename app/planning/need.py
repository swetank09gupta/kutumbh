"""Spending and the retirement living cost, derived from the transactions (never typed)."""
from __future__ import annotations

from datetime import date

from sqlalchemy import text
from sqlalchemy.orm import Session

# Not part of the cost of living once retired (docs/PLANNING.md §3): the loan ends, the term premium ends, children are funded by their
# own goals, family support is parents' care covered by the health goal. Everything else (incl. the large unlabelled "other") stays in.
# Health (medical bills) is planned by the Health & senior care goal from each person's retirement, so it is not counted twice here.
RETIREMENT_EXCLUDED = {("emi", "emi_regular"), ("insurance", "term_insurance"), ("child_expenses", None), ("family_support", None), ("medical", None)}


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


def spending_trend(db: Session, today: date | None = None, windows: int = 3) -> dict:
    """Core spending (the retirement-relevant part) in the last `windows` 12-month windows, oldest first, and its yearly growth.
    The growth is only given when every window holds spending in all 12 months, so a year with thin statement coverage is not read as a fall."""
    today = today or date.today()
    end = month_window(today)[1]
    rows = db.execute(text(
        "SELECT category, subcategory, month, SUM(amount) FROM v_cashflow WHERE flow = 'expense' AND NOT provisional AND owner_id IN ('user', 'wife') "
        "AND month >= :s AND month < :e GROUP BY 1, 2, 3"), {"s": date(end.year - windows, end.month, 1), "e": end}).all()
    out = []
    for k in range(windows - 1, -1, -1):                                   # oldest first
        w_end = date(end.year - k, end.month, 1)
        w_start = date(w_end.year - 1, w_end.month, 1)
        in_w = [(c, sub, float(a)) for c, sub, m, a in rows if w_start <= m < w_end]
        months = len({m for _c, _s, m, _a in rows if w_start <= m < w_end})
        out.append({"start": w_start.isoformat(), "end": w_end.isoformat(), "months": months, "core": split_spending(in_w)["retirement_core"]})
    usable = all(w["months"] >= 12 and w["core"] > 0 for w in out)
    growth = (out[-1]["core"] / out[0]["core"]) ** (1 / (windows - 1)) - 1 if usable else None
    return {"windows": out, "growth_pct": round(growth * 100, 1) if growth is not None else None}


def drift_from_growth(growth_pct: float | None, general_pct: float, cap_pp: float) -> float:
    """Own spending growth above prices that the plan carries forward, as a fraction: none when it is unknown or below prices (a fall is
    not counted on), at most `cap_pp` when above."""
    if growth_pct is None:
        return 0.0
    return min(max(growth_pct - general_pct, 0.0), cap_pp) / 100
