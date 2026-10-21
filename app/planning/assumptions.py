"""
The assumptions a simulation runs on. `Assumptions` is a plain bag of numbers; `derive()` (app/planning/expectations.py) fills it from
live market data, history and dated public parameters. Returns are nominal, before tax, net of fund cost; the tax drags are set per goal
from each holder's regime and income (app/planning/taxes.py). Scenarios shift the market together (docs/PLANNING.md §2).
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace


@dataclass(frozen=True)
class Assumptions:
    equity: float = 0.105
    gold: float = 0.07
    debt: float = 0.07
    ppf: float = 0.071
    tax_drag: dict = field(default_factory=lambda: {"equity": 0.006, "gold": 0.005, "debt": 0.018, "ppf": 0.0})
    retirement_tax_drag: dict = field(default_factory=lambda: {"equity": 0.003, "gold": 0.003, "debt": 0.009, "ppf": 0.0})
    inflation_general: float = 0.055
    inflation_sd: float = 0.018
    inflation_medical: float = 0.115
    inflation_education: float = 0.09          # domestic
    inflation_education_abroad: float = 0.075
    inflation_wedding: float = 0.075
    medical_weight_in_retirement: float = 0.15
    salary_growth: float = 0.09
    equity_vol_scale: float | None = None      # None: derived from the 20-year / joint-window volatility ratio
    n_paths: int = 3000
    block_months: int = 6
    seed: int = 20261021

    def after_tax(self, asset: str, retired: bool = False) -> float:
        return getattr(self, asset) - (self.retirement_tax_drag if retired else self.tax_drag).get(asset, 0.0)

    @property
    def retirement_inflation(self) -> float:
        w = self.medical_weight_in_retirement
        return (1 - w) * self.inflation_general + w * self.inflation_medical


SCENARIOS = {"cautious": "weaker returns, higher inflation", "base": "", "hopeful": "stronger returns, lower inflation"}


def with_drags(a: Assumptions, saving: dict, retired: dict) -> Assumptions:
    return replace(a, tax_drag=saving, retirement_tax_drag=retired)
