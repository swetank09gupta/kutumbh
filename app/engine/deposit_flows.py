"""
Dated cash flows of a PPF account, rebuilt from what the statements print.

The monthly bank statement shows the PPF balance every month. Its steps ARE the contributions, plus the interest
credited on 31 March. Bank transfers to the PPF (dated, exact) are used where the bank statement has them; whatever
the balance rose by beyond bank transfers and interest is a contribution made in a month whose bank statement is
missing — dated to the middle of that gap and flagged as such. Interest is computed by PPF's own rule
(app.engine.small_savings) at the notified rate of each month.
"""
from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from typing import Callable

from app.engine.small_savings import ppf_rate_on

ZERO = Decimal(0)


def _march_ends(a: date, b: date) -> list[date]:
    return [date(y, 3, 31) for y in range(a.year, b.year + 1) if a < date(y, 3, 31) <= b]


def _fy_interest(opening: Decimal, contribs: list[tuple[date, Decimal]], fy_end: date, rate_on) -> Decimal:
    """Interest credited at `fy_end` (31 March) by the 5th-to-month-end rule, month by month through the FY."""
    interest, bal = ZERO, opening
    y, m = fy_end.year - 1, 4
    for _ in range(12):
        first, fifth = date(y, m, 1), date(y, m, 5)
        in_month = [(d, a) for d, a in contribs if d.year == y and d.month == m]
        low = bal + sum((a for d, a in in_month if d <= fifth), ZERO)
        interest += low * Decimal(str(rate_on(first))) / Decimal(1200)
        bal += sum((a for _, a in in_month), ZERO)
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return interest


def _infer_unlisted_deposit(x: Decimal, rate: float) -> tuple[Decimal, int, str]:
    """
    A balance rise the bank transfers and known interest do not explain, X = R + interest on R, is a deposit R
    whose interest depends on how many months n it earned (n = 12: paid by 5 April, n = 0: after 5 March).
    People pay round amounts, so an n for which R is a multiple of ₹1,000 (within ₹2) is the month. 'high' only when
    exactly ONE n fits that tightly (a looser fit is chance for some month); a unique fit within ₹15 is 'medium';
    otherwise the best guess is returned as 'low' and the month is a guess.
    """
    fits = []
    for n in range(0, 13):
        r = x / (1 + Decimal(str(rate)) * n / 1200)
        fits.append((abs(r - (r / 1000).quantize(Decimal(1)) * 1000), n, r))
    for limit, label in ((2, "high"), (15, "medium")):
        good = [f for f in fits if f[0] <= limit]
        if len(good) == 1:
            _, n, r = good[0]
            return (r / 1000).quantize(Decimal(1)) * 1000, n, label
    return (x / (1 + Decimal(str(rate)) * 6 / 1200)).quantize(Decimal("0.01")), 6, "low"


def _deposit_date(fy_start_year: int, n: int) -> date:
    """A deposit that earned n months this financial year (12: paid by 5 April ... 0: after 5 March)."""
    if n >= 12:
        return date(fy_start_year, 4, 3)
    idx = 12 - n                                       # months after April
    y, m = fy_start_year + (3 + idx) // 12, (3 + idx) % 12 + 1
    return date(y, m, 3) if n > 0 else date(fy_start_year + 1, 3, 20)


def ppf_measure(snapshots: list[tuple[date, Decimal]], bank_transfers: list[tuple[date, Decimal]],
                rate_on: Callable[[date], float] = ppf_rate_on,
                maybe: list[tuple[date, Decimal]] | None = None) -> dict | None:
    """
    A PPF's return, measured only over whole financial years the statements anchor at both ends (interest is
    credited ONLY on 31 March, so a 31-March balance is a settled figure and nothing is assumed in between).

    Takes the latest run of consecutive 31-March balances. For each year it reconciles
        opening + deposits + interest by PPF's rule (5th-to-month-end, notified rate) = closing
    using the bank transfers for the deposits. A rise those do not explain is searched for first among `maybe`
    (round, unlabelled own-account transfers: a PPF deposit whose narration does not say PPF) and accepted only when
    it closes the year; what is still unexplained is a deposit the bank statements don't show, its month inferred from
    the interest it earned (round-amount fit) and flagged.
    Returns {flows: [(date, put in)], terminal: (date, balance), years: [...], notes: [...]} or None.
    """
    march = {d.year: b for d, b in sorted(snapshots) if d.month == 3 and d.day == 31}
    if len(march) < 2:
        return None
    y_end = max(march)
    y0 = y_end
    while (y0 - 1) in march:
        y0 -= 1
    if y0 == y_end:
        return None
    flows: list[tuple[date, Decimal]] = [(date(y0, 3, 31), march[y0])]
    years, notes = [], []
    for y in range(y0, y_end):
        start, end = date(y, 3, 31), date(y + 1, 3, 31)
        opening, closing = march[y], march[y + 1]
        dated = [(d, a) for d, a in bank_transfers if start < d <= end]
        known = _fy_interest(opening, dated, end, rate_on)
        x = closing - opening - sum((a for _, a in dated), ZERO) - known
        row = {"year": f"{y}-{str(y + 1)[2:]}", "opening": float(opening), "closing": float(closing),
               "bank_deposits": float(sum((a for _, a in dated), ZERO)), "interest": float(known.quantize(Decimal("0.01"))),
               "unexplained": 0.0, "status": "reconciled", "inferred": None}
        tol = max(Decimal(5), closing * Decimal("0.0005"))
        if x > tol:
            for cd, ca in sorted((c for c in (maybe or []) if start < c[0] <= end), key=lambda c: -c[1]):
                with_c = dated + [(cd, ca)]
                x1 = closing - opening - sum((a for _, a in with_c), ZERO) - _fy_interest(opening, with_c, end, rate_on)
                if abs(x1) <= tol or (x1 > tol and _infer_unlisted_deposit(x1, rate_on(date(y, 4, 1)))[2] == "high"):
                    dated, x = with_c, x1
                    row["found_among_transfers"] = row.get("found_among_transfers", []) + [f"{cd:%d %b %Y} ₹{ca:,.0f}"]
                    known = _fy_interest(opening, dated, end, rate_on)
                    row["bank_deposits"] = float(sum((a for _, a in dated), ZERO))
                    row["interest"] = float(known.quantize(Decimal("0.01")))
                    if abs(x) <= tol:
                        break
        flows += dated
        if abs(x) > max(Decimal(5), closing * Decimal("0.0005")) and x > 0:
            r, n, conf = _infer_unlisted_deposit(x, rate_on(date(y, 4, 1)))
            when = _deposit_date(y, n)
            flows.append((when, r))
            row.update(unexplained=float(r), status="deposit not on a bank statement" if conf == "high"
                       else "deposit not on a bank statement, month uncertain",
                       inferred={"amount": float(r), "month": when.strftime("%b %Y"), "confidence": conf})
            notes.append(f"{row['year']}: ₹{r:,.0f} deposited {'in' if conf == 'high' else 'around (month uncertain)'} "
                         f"{when:%b %Y} is not on a bank statement")
        elif x < -max(Decimal(5), closing * Decimal("0.0005")):
            row.update(unexplained=float(x), status="balance lower than deposits + interest explain")
            notes.append(f"{row['year']}: balance ₹{-x:,.0f} lower than deposits + interest explain (a withdrawal?)")
        years.append(row)
    return {"flows": flows, "terminal": (date(y_end, 3, 31), march[y_end]), "years": years, "notes": notes}


# ── fixed deposits ───────────────────────────────────────────────────────────
def fd_flows(snapshots: list[tuple[date, Decimal, Decimal]], open_date: date | None,
             bank_credits: list[tuple[date, Decimal]]) -> list[tuple[date, Decimal]]:
    """
    Signed flows (money in negative, money out positive) of one deposit, from its monthly statement rows
    (as_on, principal, interest accrued):

      * opening: the first principal (+ accrued) — on the open date when the history starts at it;
      * principal falls  -> a part-withdrawal; dated by the bank credit of that size (interest included) in
                            the month, else the middle of the month;
      * principal rises by about the interest accrued -> interest added to the principal on its anniversary,
                            not a flow; a rise of any other size is a top-up.
    The deposit's value today is added by the caller.
    """
    snaps = sorted(snapshots)
    if not snaps:
        return []
    d0, p0, a0 = snaps[0]
    accrued0 = a0 or ZERO
    if open_date and 0 <= (d0 - open_date).days <= 35:
        flows = [(open_date, -p0)]
    else:
        flows = [(d0, -(p0 + accrued0))]            # history starts mid-life: value at the first statement
    for (a_d, a_p, a_acc), (b_d, b_p, _b_acc) in zip(snaps, snaps[1:]):
        dp = b_p - a_p
        if abs(dp) <= 1:
            continue
        if dp < 0:
            gone = -dp
            match = next((amt for d, amt in bank_credits if a_d < d <= b_d and gone - 1 <= amt <= gone * Decimal("1.03") + 1), None)
            when = next((d for d, amt in bank_credits if a_d < d <= b_d and amt == match), None) if match else None
            flows.append((when or (a_d + (b_d - a_d) / 2), match if match else gone))
        else:
            accrued = a_acc or ZERO
            if accrued * Decimal("0.8") <= dp <= accrued * Decimal("1.25") + 50:
                continue                                      # interest added to the principal
            flows.append((a_d + (b_d - a_d) / 2, -dp))
    return flows
