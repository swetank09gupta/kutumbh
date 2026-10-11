"""
XIRR and CAGR computation engine.

XIRR is the root of the NPV equation (bracketed, then bisected):
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
    tol: float = 1e-9,
    max_iter: int = 200,
) -> Optional[float]:
    """
    Returns annualised XIRR as a percentage (e.g. 14.2 for 14.2%), or None when the
    cash flows have no rate in (-99.9%, +1000%] — all one sign, or money that went in
    and came out within days so any rate "fits".

    Solved by bracketing, not Newton: Newton diverges on cash-flow streams that
    change sign several times (a family's combined trades), which silently turned
    valid returns into "—". `guess` is kept for API compatibility.
    """
    if len(cashflows) < 2:
        return None
    amounts = [c for _, c in cashflows]
    if all(a >= 0 for a in amounts) or all(a <= 0 for a in amounts):
        return None

    t0 = min(d for d, _ in cashflows)
    pts = [((d - t0).days / 365.25, c) for d, c in cashflows]

    def npv(r: float) -> float:
        base = 1.0 + r
        return sum(c / base ** t for t, c in pts)

    # Scan upward from deep loss to 1000% for the first sign change, then bisect inside it.
    grid = [-0.999, -0.99, -0.95, -0.9, -0.8, -0.6, -0.4, -0.2, -0.1, -0.05, 0.0, 0.05, 0.1, 0.2,
            0.35, 0.5, 0.75, 1.0, 2.0, 4.0, 7.0, 10.0]
    try:
        prev_r, prev_f = grid[0], npv(grid[0])
        for r in grid[1:]:
            f = npv(r)
            if prev_f == 0.0:
                return round(prev_r * 100, 2)
            if prev_f * f < 0:
                lo, hi, flo = prev_r, r, prev_f
                for _ in range(max_iter):
                    mid = (lo + hi) / 2.0
                    fm = npv(mid)
                    if abs(hi - lo) < tol:
                        break
                    if flo * fm <= 0:
                        hi = mid
                    else:
                        lo, flo = mid, fm
                return round(((lo + hi) / 2.0) * 100, 2)
            prev_r, prev_f = r, f
    except (OverflowError, ZeroDivisionError, ValueError):
        return None
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
