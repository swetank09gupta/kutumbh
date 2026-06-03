"""
XIRR and CAGR computation engine.

XIRR is computed via Newton-Raphson iteration over the NPV equation:
  NPV(r) = Σ CF_i / (1+r)^((t_i - t_0) / 365.25) = 0

Cash-flow sign convention (matches finance standard):
  Outflows (buy / invest) → negative
  Inflows  (sell / redeem / terminal current value) → positive
"""
from __future__ import annotations

import math
from datetime import date
from typing import Optional


def xirr(
    cashflows: list[tuple[date, float]],
    guess: float = 0.15,
    tol: float = 1e-7,
    max_iter: int = 500,
) -> Optional[float]:
    """
    Returns annualised XIRR as a percentage (e.g. 14.2 for 14.2%), or None if
    the cashflow series is degenerate / doesn't converge.

    cashflows: list of (date, signed_amount) tuples.
    """
    if len(cashflows) < 2:
        return None

    # Need at least one negative and one positive cash flow
    amounts = [c for _, c in cashflows]
    if all(a >= 0 for a in amounts) or all(a <= 0 for a in amounts):
        return None

    t0 = min(d for d, _ in cashflows)
    years = [(d - t0).days / 365.25 for d, _ in cashflows]

    def npv(r: float) -> float:
        if r <= -1:
            return float("inf")
        return sum(c / (1.0 + r) ** t for (_, c), t in zip(cashflows, years))

    def dnpv(r: float) -> float:
        if r <= -1:
            return float("inf")
        return sum(-t * c / (1.0 + r) ** (t + 1) for (_, c), t in zip(cashflows, years))

    # Try multiple starting guesses if first doesn't converge
    for start in [guess, 0.0, -0.05, 0.5, -0.5]:
        r = start
        try:
            for _ in range(max_iter):
                f = npv(r)
                fp = dnpv(r)
                if abs(fp) < 1e-14:
                    break
                step = f / fp
                r -= step
                if abs(step) < tol:
                    # Guard: result must be sane (between -100% and +1000%)
                    if -1.0 < r < 10.0:
                        return round(r * 100, 2)
                    break
        except (OverflowError, ZeroDivisionError, ValueError):
            continue

    return None


def cagr(
    start_value: float,
    end_value: float,
    years: float,
) -> Optional[float]:
    """
    Returns annualised CAGR as a percentage, or None if inputs are invalid.
    """
    if start_value <= 0 or end_value <= 0 or years < 0.08:
        return None
    return round(((end_value / start_value) ** (1.0 / years) - 1.0) * 100, 2)


def total_return_pct(invested: float, current: float) -> Optional[float]:
    """Absolute (non-annualised) return as a percentage."""
    if invested <= 0:
        return None
    return round((current / invested - 1.0) * 100, 2)


def fd_maturity_cagr(principal: float, rate_pct: float, years: float) -> Optional[float]:
    """
    Forward CAGR for an interest-bearing instrument (FD/PPF/RD) using
    the stated annual rate compounded annually.
    Returns the same rate_pct but validates the inputs first.
    """
    if principal <= 0 or rate_pct <= 0 or years < 0:
        return None
    return round(rate_pct, 2)


def holding_cagr(
    avg_cost_nav: float,
    current_nav: float,
    first_buy_date: Optional[date],
    today: Optional[date] = None,
) -> Optional[float]:
    """
    CAGR for a holding using avg_cost_nav as proxy for purchase price.
    Uses first_buy_date to determine holding period.
    """
    if not first_buy_date:
        return None
    td = today or date.today()
    years = (td - first_buy_date).days / 365.25
    return cagr(avg_cost_nav, current_nav, years)


def build_investment_cashflows(
    transactions: list[dict],
    current_value: float,
    today: Optional[date] = None,
) -> list[tuple[date, float]]:
    """
    Convert a list of transaction dicts into signed cash flows for XIRR.

    Expected dict keys: 'date' (date), 'amount' (float), 'transaction_type' (str).
      transaction_type='debit'  → outflow (buy) → negative CF
      transaction_type='credit' → inflow (sell/dividend) → positive CF

    Appends current_value as a terminal positive cash flow at today's date.
    """
    cfs: list[tuple[date, float]] = []
    for t in transactions:
        amt = float(t["amount"])
        typ = t["transaction_type"]
        dt  = t["date"] if isinstance(t["date"], date) else t["date"]
        if typ == "debit":
            cfs.append((dt, -abs(amt)))
        elif typ == "credit":
            cfs.append((dt, abs(amt)))

    if current_value > 0:
        cfs.append((today or date.today(), float(current_value)))

    return cfs
