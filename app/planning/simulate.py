"""
Monte Carlo of a goal portfolio on resampled real monthly returns (docs/PLANNING.md §1).

One calculation covers every goal. A goal is a stream of contributions (a shape over the months, scaled by a monthly SIP) and a stream
of outflows (a single cost on a date, staged tuition, or a lifetime of retirement spending). Discounting at each path's realised returns,
the plan survives on that path iff for every month

    corpus0 + scale * B(m)  >=  O(m)          B(m) = PV of the contribution shape up to m,  O(m) = PV of the outflows up to m

so the smallest workable scale on a path is max_m (O(m) - corpus0) / B(m). The chance of a plan is the share of paths it covers, and the
SIP for p-confidence is the p-quantile of the per-path scales: no search, no noise between runs (fixed seed).
"""
from __future__ import annotations

from typing import Callable

import numpy as np
import pandas as pd

from app.planning.assumptions import Assumptions

ASSET_ORDER = ("equity", "gold", "debt", "ppf")
CATEGORIES = ("general", "medical", "education", "education_abroad", "wedding", "basket")


def calibrate(joint: pd.DataFrame, equity_long: pd.Series, a: Assumptions, retired: bool = False) -> tuple[np.ndarray, np.ndarray, float]:
    """(deviations (n, 3), forward monthly mean (3,), equity vol scale). Deviations are the real months minus their own mean, so
    the path keeps real crashes, recoveries and co-movement but its average is the forward assumption, not the past's luck."""
    scale = a.equity_vol_scale or float(equity_long.std() / joint["equity"].std())
    cols = joint[["equity", "gold", "debt"]]
    dev = (cols - cols.mean()).to_numpy().copy()
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
    return inflation_paths({"x": mean}, sd, n_paths, n_months, rng)["x"]


def inflation_paths(means: dict[str, float], sd: float, n_paths: int, n_months: int, rng) -> dict[str, np.ndarray]:
    """Cumulative price factors per category sharing one yearly shock (they rise and fall together, as prices do)."""
    years = -(-n_months // 12)
    z = rng.standard_normal(size=(n_paths, years)) if sd > 0 else np.zeros((n_paths, years))
    out = {}
    for cat, mean in means.items():
        annual = np.clip(mean + sd * z, 0.01, 0.20)
        monthly = np.repeat((1 + annual) ** (1 / 12), 12, axis=1)[:, :n_months]
        out[cat] = np.concatenate([np.ones((n_paths, 1)), np.cumprod(monthly, axis=1)], axis=1)
    return out


def glide_weights(start: dict, end: dict, n_months: int, glide_months: int, glide_end: int | None = None) -> np.ndarray:
    """(n_months, 4) weights equity/gold/debt/ppf: constant, then a straight line to `end` over `glide_months` ending at `glide_end`
    (default: the last month), then held."""
    w0 = np.array([start.get(k, 0.0) for k in ASSET_ORDER], dtype=float)
    w1 = np.array([end.get(k, 0.0) for k in ASSET_ORDER], dtype=float)
    w0, w1 = w0 / w0.sum(), w1 / w1.sum()
    stop = n_months if glide_end is None else min(glide_end, n_months)
    g = min(max(glide_months, 0), stop)
    frac = np.zeros(n_months)
    if g:
        frac[stop - g:stop] = (np.arange(g) + 1) / g
    frac[stop:] = 1.0                                     # after the glide the end mix is held
    return w0 + frac[:, None] * (w1 - w0)


def portfolio_returns(r3: np.ndarray, weights: np.ndarray, ppf_annual: float | np.ndarray) -> np.ndarray:
    ppf_m = (1 + np.asarray(ppf_annual)) ** (1 / 12) - 1
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


def contribution_shape(n_months: int, stepup: float, streams: list[tuple[float, int]]) -> np.ndarray:
    """(n_months+1,) monthly contribution of the given streams [(monthly amount, last month), ...] raised by `stepup` each year."""
    m = np.arange(n_months + 1)
    shape = np.zeros(n_months + 1)
    for amount, last in streams:
        shape += np.where(m < last, amount * (1 + stepup) ** (m // 12), 0.0)
    return shape


def solve(r: np.ndarray, shape: np.ndarray, outflow: np.ndarray, corpus0: float) -> dict:
    """Per-path smallest scale of `shape` that keeps wealth >= 0 after every outflow. r (n_paths, n); shape (n+1,); outflow (n_paths, n+1)."""
    n_paths, n = r.shape
    D = np.ones((n_paths, n + 1))
    D[:, 1:] = 1.0 / np.cumprod(1 + r, axis=1)
    B = np.cumsum(shape[None, :] * D, axis=1)
    O = np.cumsum(outflow * D, axis=1)
    with np.errstate(divide="ignore", invalid="ignore"):
        need = np.where(B > 0, (O - corpus0) / B, np.where(O > corpus0, np.inf, -np.inf))
    return {"need": np.clip(need.max(axis=1), 0.0, None), "D": D, "B": B, "O": O}


def build_returns(data: dict, a: Assumptions, weights: np.ndarray, retire_month: int | None, n_paths: int, rng) -> np.ndarray:
    """Portfolio monthly returns (n_paths, n): the saving-years tax drag before `retire_month`, the retired drag after."""
    n = len(weights)
    dev, mu, _ = calibrate(data["joint"], data["equity_long"], a)
    _, mu_ret, _ = calibrate(data["joint"], data["equity_long"], a, retired=True)
    r3 = draw_returns(dev, np.zeros(3), n_paths, n, a.block_months, rng)
    cut = n if retire_month is None else min(max(retire_month, 0), n)
    r3[:, :cut] += mu
    r3[:, cut:] += mu_ret
    ppf = np.where(np.arange(n) < cut, a.after_tax("ppf"), a.after_tax("ppf", True))
    return portfolio_returns(r3, weights, ppf)


def simulate_plan(*, corpus0: float, n_months: int, shape: np.ndarray, scale_now: float, outflow_fn: Callable, weights: np.ndarray,
                  retire_month: int | None, data: dict, a: Assumptions, confidences=(0.5, 0.8, 0.9), inflation_means: dict | None = None) -> dict:
    """Run one plan. `outflow_fn(infl, n_paths) -> (n_paths, n_months+1)` builds the nominal outflows from the per-category price factors."""
    rng = np.random.default_rng(a.seed)
    r = build_returns(data, a, weights, retire_month, a.n_paths, rng)
    means = inflation_means or {"general": a.inflation_general, "medical": a.inflation_medical, "education": a.inflation_education,
                                "education_abroad": a.inflation_education_abroad, "wedding": a.inflation_wedding, "basket": a.retirement_inflation}
    infl = inflation_paths(means, a.inflation_sd, a.n_paths, n_months, rng)
    out = outflow_fn(infl, a.n_paths)
    s = solve(r, shape, out, corpus0)
    need = s["need"]
    final = (corpus0 + scale_now * s["B"][:, -1] - s["O"][:, -1]) / s["D"][:, -1]
    q = lambda c: (lambda v: float(v) if np.isfinite(v) else None)(np.quantile(need, c))          # None: no contribution can cover it
    return {"probability": float((scale_now >= need).mean()),
            "scale_for_confidence": {f"{int(c * 100)}": q(c) for c in confidences},
            "final_wealth": summarize(final), "paths": a.n_paths, "need": need, "D": s["D"], "infl": infl}


# ── wrappers keeping the earlier two-function interface (and its closed-form tests) ─────────────────────────────────────────────────────────


def simulate_accumulation(*, corpus0: float, sip0: float, stepup: float, years: float, target_today: float, goal_inflation: float,
                          start_weights: dict, end_weights: dict, glide_years: float, data: dict, a: Assumptions,
                          confidences=(0.5, 0.8, 0.9)) -> dict:
    """A cost on a date (today's money, grown at `goal_inflation`) reached by saving a SIP raised by `stepup` each year."""
    n = max(1, round(years * 12))
    w = glide_weights(start_weights, end_weights, n, round(glide_years * 12))
    shape = contribution_shape(n, stepup, [(1.0, n)])

    rng_infl = {"x": goal_inflation}

    def outflow(infl, n_paths):
        o = np.zeros((n_paths, n + 1))
        o[:, n] = target_today * infl["x"][:, n]
        return o

    res = simulate_plan(corpus0=corpus0, n_months=n, shape=shape, scale_now=sip0, outflow_fn=outflow, weights=w, retire_month=None,
                        data=data, a=a, confidences=confidences, inflation_means=rng_infl)
    target = target_today * res["infl"]["x"][:, n]
    final = res["final_wealth"]
    return {"years": n / 12, "probability": res["probability"], "target_future": summarize(target),
            "corpus_future": _corpus(res, corpus0, sip0, shape, target),
            "sip_for_confidence": res["scale_for_confidence"], "equity_vol_scale": calibrate(data["joint"], data["equity_long"], a)[2]}


def _corpus(res: dict, corpus0: float, sip0: float, shape: np.ndarray, target: np.ndarray) -> dict:
    """What the plan accumulates (before the cost): corpus0 grown plus the SIP's contributions grown, per path, summarised."""
    D = res["D"]
    B_end = np.cumsum(shape[None, :] * D, axis=1)[:, -1]
    return summarize((corpus0 + sip0 * B_end) / D[:, -1])


def simulate_retirement(*, corpus0: float, sip0: float, stepup: float, years_to_retire: float, years_in_retirement: float,
                        spend_today: float, start_weights: dict, retire_weights: dict, glide_years: float, data: dict, a: Assumptions,
                        confidences=(0.8, 0.9, 0.95)) -> dict:
    """Accumulate to retirement, then withdraw `spend_today` (inflated by the retirement basket) monthly for years_in_retirement."""
    n_acc, n_dec = max(1, round(years_to_retire * 12)), max(1, round(years_in_retirement * 12))
    n = n_acc + n_dec
    w = glide_weights(start_weights, retire_weights, n, round(glide_years * 12), glide_end=n_acc)
    shape = contribution_shape(n, stepup, [(1.0, n_acc)])

    def outflow(infl, n_paths):
        o = np.zeros((n_paths, n + 1))
        o[:, n_acc:n] = spend_today / 12 * infl["basket"][:, n_acc:n]
        return o

    res = simulate_plan(corpus0=corpus0, n_months=n, shape=shape, scale_now=sip0, outflow_fn=outflow, weights=w, retire_month=n_acc,
                        data=data, a=a, confidences=confidences)
    D, infl = res["D"], res["infl"]
    spend = np.zeros((a.n_paths, n + 1)); spend[:, n_acc:n] = spend_today / 12 * infl["basket"][:, n_acc:n]
    required = (spend[:, n_acc:n] * D[:, n_acc:n] / D[:, n_acc:n_acc + 1]).sum(axis=1)          # corpus at retirement that exactly lasts
    deflate = (1 + a.inflation_general) ** years_to_retire
    final = (corpus0 + sip0 * np.cumsum(shape[None, :] * D, axis=1)[:, n_acc] ) / D[:, n_acc]
    return {"years_to_retire": n_acc / 12, "years_in_retirement": n_dec / 12, "probability": res["probability"],
            "corpus_at_retirement": summarize(final),
            "required_corpus": {f"{int(c * 100)}": float(np.quantile(required, c)) for c in confidences},
            "required_corpus_today_money": {f"{int(c * 100)}": float(np.quantile(required, c) / deflate) for c in confidences},
            "sip_for_confidence": res["scale_for_confidence"],
            "spend_at_retirement_year": float(np.median(spend[:, n_acc] * 12)), "equity_vol_scale": calibrate(data["joint"], data["equity_long"], a)[2]}
