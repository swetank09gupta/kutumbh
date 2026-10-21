"""
Prepay the loan or invest? The loan's rate is a certain, tax-free-in-effect return (the 24(b) cap is already used by the interest).
Against it: what a portfolio earned after tax in the simulated futures, for the borrower's own tax position (docs/PLANNING.md).
"""
from __future__ import annotations

import numpy as np

from app.planning import simulate, taxes
from app.planning.goal_plan import Context

MIXES = {"all equity": {"equity": 1.0}, "60/40 equity/debt": {"equity": 0.6, "debt": 0.4}, "debt funds / FDs": {"debt": 1.0}}


def compare(ctx: Context, rate_pct: float, owner: str = "user", years: int = 10) -> dict:
    a0 = ctx.base
    prof = ctx.profiles.get(owner) or next(iter(ctx.profiles.values()), None)
    debt_rate = prof["marginal_rate"] if prof else 0.31
    fy = prof["fy"] if prof else "2026-27"
    saving, retired = taxes.drags(a0, years, debt_rate, ctx.spending["retirement_core"] / 2, ctx.today, fy)
    from app.planning.assumptions import with_drags
    a = with_drags(a0, saving, retired)
    n = years * 12
    rng = np.random.default_rng(a.seed)
    rate = rate_pct / 100
    out = {}
    for label, mix in MIXES.items():
        w = simulate.glide_weights(mix, mix, n, 0)
        r = simulate.build_returns(ctx.data, a, w, None, a.n_paths, np.random.default_rng(a.seed))
        fin = np.prod(1 + r, axis=1)
        cagr = fin ** (1 / years) - 1
        out[label] = {"beats_loan": float((cagr >= rate).mean()), "p10_pct": float(np.percentile(cagr, 10) * 100), "p50_pct": float(np.percentile(cagr, 50) * 100)}
    return {"years": years, "loan_rate_pct": rate_pct, "borrower_debt_tax_pct": round(debt_rate * 100, 1),
            "debt_after_tax_pct": round(a.after_tax("debt") * 100, 2), "mixes": out}
