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


def ppf_flows(snapshots: list[tuple[date, Decimal]], bank_transfers: list[tuple[date, Decimal]],
              rate_on: Callable[[date], float] = ppf_rate_on) -> tuple[list[tuple[date, Decimal]], list[str]]:
    """
    [(date, amount put in)] and notes. The first balance is the opening position (everything before the first
    statement), dated at that statement. Returns ([], notes) for fewer than two statements.
    """
    snaps = sorted(snapshots)
    # Start at the first 31-March balance: the interest of a part-year before the first statement is unknowable,
    # so measuring from a financial year-end keeps every counted year whole.
    start = next((i for i, (d, _) in enumerate(snaps) if d.month == 3 and d.day == 31), 0)
    snaps = snaps[start:]
    if len(snaps) < 2:
        return [], ["fewer than two statements: no history"]
    notes: list[str] = []
    flows: list[tuple[date, Decimal]] = [(snaps[0][0], snaps[0][1])]
    fy_open = snaps[0][1]                       # balance after the last 31-March credit (or the first balance)
    fy_contribs: list[tuple[date, Decimal]] = []
    for (a_d, a_b), (b_d, b_b) in zip(snaps, snaps[1:]):
        dated = [(d, amt) for d, amt in bank_transfers if a_d < d <= b_d]
        interest = ZERO
        for fy_end in _march_ends(a_d, b_d):
            interest += _fy_interest(fy_open, fy_contribs + [x for x in dated if x[0] <= fy_end], fy_end, rate_on)
        residual = (b_b - a_b) - sum((amt for _, amt in dated), ZERO) - interest
        flows += dated
        fy_contribs += dated
        # a balance rise bank transfers and interest do not explain: a deposit in a month with no bank statement
        if residual > max(Decimal(2000), Decimal("0.01") * b_b):
            mid = a_d + (b_d - a_d) / 2
            flows.append((mid, residual.quantize(Decimal("0.01"))))
            fy_contribs.append((mid, residual))
            notes.append(f"{residual:,.0f} deposited between {a_d} and {b_d} (no bank statement for those months)")
        if _march_ends(a_d, b_d):
            fy_open = b_b
            fy_contribs = [x for x in fy_contribs if x[0] > _march_ends(a_d, b_d)[-1]]
    return flows, notes


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
