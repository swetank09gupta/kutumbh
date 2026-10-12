"""
Amortisation of a monthly-rest floating loan (HDFC): each month interest = balance x annual rate / 12, the EMI pays that
interest first and the rest reduces the balance. Pure functions, no database.

Checked against the lender: from one prepayment letter's principal, one EMI reproduces the next letter's figure to the
rupee (tests/test_loan_schedule.py, and the live reconciliation in app/engine/loan_report.py).
"""
from __future__ import annotations

from datetime import date
from decimal import ROUND_HALF_UP, Decimal

D = Decimal
MAX_MONTHS = 1200


def q(x: Decimal) -> Decimal:
    return x.quantize(D("0.01"), rounding=ROUND_HALF_UP)


def interest_for(balance: Decimal, rate_pct) -> Decimal:
    return balance * D(str(rate_pct)) / D(1200)


def step(balance: Decimal, rate_pct, emi: Decimal) -> tuple[Decimal, Decimal, Decimal]:
    """One instalment: (interest, principal repaid, new balance)."""
    interest = interest_for(balance, rate_pct)
    principal = emi - interest
    return interest, principal, balance - principal


def payoff(balance: Decimal, rate_pct, emi: Decimal) -> dict | None:
    """Instalments needed to clear `balance` at a fixed rate and EMI (the last one may be smaller), the interest paid
    over them, and that last EMI. None when the EMI does not even cover the interest."""
    balance, emi = D(balance), D(emi)
    if balance <= 0:
        return {"months": 0, "interest": D(0), "last_emi": D(0)}
    if emi <= interest_for(balance, rate_pct):
        return None
    total, months = D(0), 0
    while balance > 0 and months < MAX_MONTHS:
        interest = interest_for(balance, rate_pct)
        months += 1
        if balance + interest <= emi:
            total += interest
            return {"months": months, "interest": q(total), "last_emi": q(balance + interest)}
        total += interest
        balance -= emi - interest
    return None


def emi_for(balance: Decimal, rate_pct, months: int) -> Decimal:
    """The level EMI that clears `balance` in `months` instalments at a fixed rate."""
    r = D(str(rate_pct)) / D(1200)
    if r == 0:
        return q(D(balance) / months)
    f = (1 + r) ** months
    return q(D(balance) * r * f / (f - 1))


def add_months(d: date, n: int) -> date:
    y, m = divmod(d.year * 12 + d.month - 1 + n, 12)
    last = [31, 29 if (y % 4 == 0 and (y % 100 != 0 or y % 400 == 0)) else 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31][m]
    return date(y, m + 1, min(d.day, last))


def payoff_date(first_emi: date, months: int) -> date:
    """Date of the final instalment when the first remaining one falls on `first_emi`."""
    return add_months(first_emi, months - 1)


def interest_saved(balance_before: Decimal, prepaid: Decimal, rate_pct, emi: Decimal) -> dict | None:
    """What one part-prepayment saves if the rate and EMI stay as they are: interest over the rest of the loan with and
    without it, and how many instalments it removes."""
    without, with_ = payoff(balance_before, rate_pct, emi), payoff(balance_before - prepaid, rate_pct, emi)
    if without is None or with_ is None:
        return None
    return {"interest_saved": q(without["interest"] - with_["interest"]), "months_saved": without["months"] - with_["months"]}
