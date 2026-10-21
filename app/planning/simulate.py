"""
Monte Carlo of a goal portfolio on resampled real monthly returns (docs/PLANNING.md §1).

Wealth is linear in the contribution, so each path yields A (what today's corpus grows to) and B (what a unit starting SIP grows to);
success is A + S0*B >= target, and the SIP for p-confidence is simply the p-quantile of (T - A)/B: no search, no noise between runs.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from app.planning.assumptions import Assumptions

ASSET_ORDER = ("equity", "gold", "debt", "ppf")


def calibrate(joint: pd.DataFrame, equity_long: pd.Series, a: Assumptions, retired: bool = False) -> tuple[np.ndarray, np.ndarray, float]:
    """(deviations (n, 3), forward monthly mean (3,), equity vol scale). Deviations are the real months minus their own mean, so
    the path keeps real crashes, recoveries and co-movement but its average is the forward assumption, not the past's luck."""
    scale = a.equity_vol_scale or float(equity_long.std() / joint["equity"].std())
    dev = (joint[["equity", "gold", "debt"]] - joint[["equity", "gold", "debt"]].mean()).to_numpy().copy()
    dev[:, 0] *= scale
    sigma2 = dev.var(axis=0)
    # arithmetic monthly mean so that the compound (geometric) growth equals the assumed annual rate
    mu = np.array([(1 + a.after_tax(k, retired)) ** (1 / 12) - 1 for k in ("equity", "gold", "debt")]) + sigma2 / 2
    return dev, mu, scale


def draw_returns(dev: np.ndarray, mu: np.ndarray, n_paths: int, n_months: int, block: int, rng) -> np.ndarray:
    """(n_paths, n_months, 3): circular block bootstrap of the real months."""
    n = len(dev)
    nb = -(-n_months // block)
    starts = rng.integers(0, n, size=(n_paths, nb))
    idx = ((starts[:, :, None] + np.arange(block)) % n).reshape(n_paths, nb * block)[:, :n_months]
    return mu + dev[idx]


def inflation_factor(mean: float, sd: float, n_paths: int, n_months: int, rng) -> np.ndarray:
    """(n_paths, n_months+1) cumulative price factor; one annual rate per path-year, applied monthly."""
    years = -(-n_months // 12)
    annual = np.clip(rng.normal(mean, sd, size=(n_paths, years)), 0.01, 0.15) if sd > 0 else np.full((n_paths, years), mean)
    monthly = np.repeat((1 + annual) ** (1 / 12), 12, axis=1)[:, :n_months]
    return np.concatenate([np.ones((n_paths, 1)), np.cumprod(monthly, axis=1)], axis=1)


def glide_weights(start: dict, end: dict, n_months: int, glide_months: int) -> np.ndarray:
    """(n_months, 4) weights equity/gold/debt/ppf: constant, then a straight line to `end` over the last glide_months."""
    w0 = np.array([start.get(k, 0.0) for k in ASSET_ORDER], dtype=float)
    w1 = np.array([end.get(k, 0.0) for k in ASSET_ORDER], dtype=float)
    w0, w1 = w0 / w0.sum(), w1 / w1.sum()
    g = min(max(glide_months, 0), n_months)
    frac = np.zeros(n_months)
    if g:
        frac[n_months - g:] = (np.arange(g) + 1) / g
    return w0 + frac[:, None] * (w1 - w0)


def portfolio_returns(r3: np.ndarray, weights: np.ndarray, ppf_annual: float) -> np.ndarray:
    ppf_m = (1 + ppf_annual) ** (1 / 12) - 1
    return (r3 * weights[None, :, :3]).sum(axis=2) + weights[None, :, 3] * ppf_m


def grow(r: np.ndarray, corpus0: float, sip0: float, stepup: float, start_month: int = 0, stop_month: int | None = None) -> np.ndarray:
    """End-of-horizon wealth per path: contribute at the start of each month (SIP raised by `stepup` each year), then earn r."""
    n_paths, n = r.shape
    stop = n if stop_month is None else stop_month
    w = np.full(n_paths, corpus0, dtype=float)
    for m in range(n):
        if start_month <= m < stop and sip0:
            w = w + sip0 * (1 + stepup) ** ((m - start_month) // 12)
        w = w * (1 + r[:, m])
    return w


def summarize(values: np.ndarray) -> dict:
    p = np.percentile(values, [10, 50, 90])
    return {"p10": float(p[0]), "p50": float(p[1]), "p90": float(p[2])}


def simulate_accumulation(*, corpus0: float, sip0: float, stepup: float, years: float, target_today: float, goal_inflation: float,
                          start_weights: dict, end_weights: dict, glide_years: float, data: dict, a: Assumptions,
                          confidences=(0.5, 0.8, 0.9)) -> dict:
    """A goal with a date and a cost (education, marriage, health fund, emergency)."""
    n = max(1, round(years * 12))
    rng = np.random.default_rng(a.seed)
    dev, mu, scale = calibrate(data["joint"], data["equity_long"], a)
    r3 = draw_returns(dev, mu, a.n_paths, n, a.block_months, rng)
    infl = inflation_factor(goal_inflation, a.inflation_sd, a.n_paths, n, rng)
    w = glide_weights(start_weights, end_weights, n, round(glide_years * 12))
    r = portfolio_returns(r3, w, a.after_tax("ppf"))
    A = grow(r, corpus0, 0.0, 0.0)
    B = grow(r, 0.0, 1.0, stepup)
    T = target_today * infl[:, -1]
    final = A + sip0 * B
    need = np.clip((T - A) / B, 0, None)
    return {
        "years": n / 12, "probability": float((final >= T).mean()),
        "target_future": summarize(T), "corpus_future": summarize(final),
        "sip_for_confidence": {f"{int(c * 100)}": float(np.quantile(need, c)) for c in confidences},
        "equity_vol_scale": scale,
    }


def simulate_retirement(*, corpus0: float, sip0: float, stepup: float, years_to_retire: float, years_in_retirement: float,
                        spend_today: float, start_weights: dict, retire_weights: dict, glide_years: float, data: dict, a: Assumptions,
                        confidences=(0.8, 0.9, 0.95)) -> dict:
    """Accumulate to retirement, then withdraw `spend_today` (inflated by the retirement basket) monthly for years_in_retirement."""
    n_acc, n_dec = max(1, round(years_to_retire * 12)), max(1, round(years_in_retirement * 12))
    n = n_acc + n_dec
    rng = np.random.default_rng(a.seed)
    dev, mu, scale = calibrate(data["joint"], data["equity_long"], a)
    _, mu_dec, _ = calibrate(data["joint"], data["equity_long"], a, retired=True)       # the same real months, lower tax drag once retired
    r3 = draw_returns(dev, np.zeros(3), a.n_paths, n, a.block_months, rng)
    r3[:, :n_acc] += mu
    r3[:, n_acc:] += mu_dec
    basket = inflation_factor(a.retirement_inflation, a.inflation_sd, a.n_paths, n, rng)
    w_acc = glide_weights(start_weights, retire_weights, n_acc, round(glide_years * 12))
    w_dec = np.tile(np.array([retire_weights.get(k, 0.0) for k in ASSET_ORDER], dtype=float), (n_dec, 1))
    w_dec = w_dec / w_dec.sum(axis=1, keepdims=True)
    r_acc = portfolio_returns(r3[:, :n_acc], w_acc, a.after_tax("ppf"))
    r_dec = portfolio_returns(r3[:, n_acc:], w_dec, a.after_tax("ppf", True))
    A = grow(r_acc, corpus0, 0.0, 0.0)
    B = grow(r_acc, 0.0, 1.0, stepup)
    spend = spend_today / 12 * basket[:, n_acc:n]                         # monthly spending, start of each retirement month
    disc = np.concatenate([np.ones((a.n_paths, 1)), np.cumprod(1 + r_dec, axis=1)[:, :-1]], axis=1)
    required = (spend / disc).sum(axis=1)                                 # corpus at retirement that exactly lasts, per path
    final = A + sip0 * B
    deflate = (1 + a.inflation_general) ** years_to_retire
    need = np.clip((required - A) / B, 0, None)
    return {
        "years_to_retire": n_acc / 12, "years_in_retirement": n_dec / 12,
        "probability": float((final >= required).mean()),
        "corpus_at_retirement": summarize(final), "required_corpus": {f"{int(c * 100)}": float(np.quantile(required, c)) for c in confidences},
        "required_corpus_today_money": {f"{int(c * 100)}": float(np.quantile(required, c) / deflate) for c in confidences},
        "sip_for_confidence": {f"{int(c * 100)}": float(np.quantile(need, c)) for c in confidences},
        "spend_at_retirement_year": float(np.median(spend[:, 0] * 12)),
        "equity_vol_scale": scale,
    }
