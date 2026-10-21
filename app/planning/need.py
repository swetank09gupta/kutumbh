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


def capacity(db: Session, today: date | None = None, term_policies: list[dict] | None = None, loan: dict | None = None,
             pay_from: dict[str, date] | None = None) -> dict:
    """What the household can put toward goals each month, from its own cash flow: recurring income less what it spends.

    Income: for an earner whose current pay started on a known date (`pay_from`, config `income`), the mean of their complete months of salary credits since
    then times 12 (a new employer counts at once, not diluted by the old one's months or its one-off settlements); for everyone else the last 12 months of
    salary credits. Plus the non-salary income of the last 12 months (interest, dividends). Spending is the last 12 months of the shared `expense`
    definition (EMI and school included, prepayments and investing not). Money from family (a parent's share of the loan) is a transfer, in neither.
    It rises when the home loan ends and when a fixed-term premium ends: both are dated so the plan can say when."""
    today = today or date.today()
    start, end = month_window(today)
    pay_from = pay_from or {}
    salary_yearly: dict[str, float] = {}
    basis: dict[str, str] = {}
    for owner in ("user", "wife"):
        since = pay_from.get(owner)
        rows = db.execute(text(
            "SELECT month, SUM(amount) FROM v_cashflow WHERE flow = 'income' AND subcategory = 'salary' AND owner_id = :o AND month >= :s AND month < :e GROUP BY 1 ORDER BY 1"),
            {"o": owner, "s": start, "e": end}).all()
        if since:
            full = [float(a) for m, a in rows if m > since.replace(day=1)]          # complete months after the month the new job began
            if full:
                salary_yearly[owner], basis[owner] = sum(full) / len(full) * 12, f"{len(full)} month(s) since {since.isoformat()}"
                continue
        salary_yearly[owner], basis[owner] = sum(float(a) for _m, a in rows), "last 12 months"
    other = float(db.execute(text(
        "SELECT COALESCE(SUM(amount), 0) FROM v_cashflow WHERE flow = 'income' AND COALESCE(subcategory, '') <> 'salary' AND month >= :s AND month < :e "
        "AND owner_id IN ('user', 'wife')"), {"s": start, "e": end}).scalar() or 0)
    spend = household_spending(db, today)["total"]
    income_yearly = sum(salary_yearly.values()) + other
    monthly = (income_yearly - spend) / 12
    out = {"monthly": monthly, "income_yearly": income_yearly, "spending_yearly": spend, "salary_yearly": salary_yearly, "salary_basis": basis,
           "other_income_yearly": other, "rises": []}
    if loan and loan.get("emis_left") is not None:
        y, m = today.year, today.month + int(loan["emis_left"])
        y, m = y + (m - 1) // 12, (m - 1) % 12 + 1
        out["rises"].append({"what": "the home loan ends", "when": date(y, m, 1).isoformat(), "monthly": float(loan.get("emi") or 0)})
    for t in term_policies or []:
        first = db.execute(text("SELECT MIN(date), COALESCE(AVG(amount) FILTER (WHERE date >= :s), 0) FROM v_cashflow WHERE flow = 'expense' AND category = 'insurance' "
                                "AND subcategory = 'term_insurance' AND owner_id = :o AND amount > 5000"), {"s": start, "o": t["owner"]}).first()
        if t.get("final_premium_date"):                                  # the policy's own last due date beats counting years from the first debit
            out["rises"].append({"what": "the term-plan premium ends", "when": t["final_premium_date"].replace(day=1).isoformat(), "monthly": float(first[1] or 0) if first else 0.0})
        elif first and first[0] and t.get("premium_term_years"):
            fy = first[0].year + t["premium_term_years"]
            out["rises"].append({"what": "the term-plan premium ends", "when": date(fy, first[0].month, 1).isoformat(), "monthly": float(first[1] or 0)})
    out["rises"].sort(key=lambda r: r["when"])
    return out
