"""
How long a holding (or a goal's whole portfolio) needs, from measured volatility and today's expected returns (docs/PLANNING.md §6).

A portfolio with expected return mu and volatility sigma (a year) loses money over h years with probability about Phi(-mu*sqrt(h)/sigma). It is a 1-in-20 event
after h = (1.645 * sigma / mu)^2 years. A goal that is `years` away can hold a portfolio whose h is no larger than that, so the safe share of equity,
whether a stock fits, and how much risk a goal's holdings carry all come out of the same two market numbers instead of a table. Volatilities and the factor
covariance are measured from real monthly returns (funds.py); expected returns are the planner's derived ones (expectations.py), the same for large, mid and small
caps (no extra premium is assumed for taking more risk).
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from app.planning.funds import FACTORS

Z = 1.645                     # one-sided 5% (1 in 20)
MAX_EQUITY = 0.85
MIN_YEARS = 0.25


@dataclass
class RiskModel:
    cov: np.ndarray           # annual covariance of the FACTORS
    mu: np.ndarray            # expected annual return of each factor

    def stats(self, exposure: np.ndarray, idio_var: float) -> tuple[float, float]:
        """(expected return, volatility) of a portfolio with these factor exposures (summing to ~1) and this unexplained variance."""
        return float(exposure @ self.mu), float(np.sqrt(max(exposure @ self.cov @ exposure + idio_var, 0.0)))


def horizon(mu: float, sigma: float, z: float = Z) -> float:
    """Years after which a loss is a 1-in-20 event; infinite when the expected return is not positive."""
    if sigma <= 1e-9:
        return 0.0
    if mu <= 0:
        return float("inf")
    return (z * sigma / mu) ** 2


def build(factors, assumptions) -> RiskModel:
    """From the factor return frame (monthly) and the planner's derived expected returns."""
    cov = np.cov(factors[list(FACTORS)].to_numpy().T, ddof=1) * 12
    mu = np.array([assumptions.equity, assumptions.equity, assumptions.equity, assumptions.gold, assumptions.debt])
    return RiskModel(cov, mu)


def equity_share(model: RiskModel, years: float, step: float = 0.01) -> float:
    """The largest share of Nifty 50 (the rest in gilts) whose 1-in-20 horizon fits inside `years`; at most MAX_EQUITY."""
    best = 0.0
    il, idb = FACTORS.index("large"), FACTORS.index("debt")
    for w in np.arange(0.0, MAX_EQUITY + 1e-9, step):
        e = np.zeros(len(FACTORS))
        e[il], e[idb] = w, 1 - w
        mu, sd = model.stats(e, 0.0)
        if horizon(mu, sd) <= max(years, MIN_YEARS):
            best = float(w)
    return best
