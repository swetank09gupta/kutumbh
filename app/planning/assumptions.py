"""
The forward-looking assumptions, with their basis in docs/PLANNING.md §2. Returns are nominal, before tax, net of fund cost;
`tax_drag` is subtracted. Scenarios shift the three market returns together; a goal's own typed rate overrides for that goal.
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
    # in retirement income is lower (a lower slab) and the yearly LTCG exemption covers more: about half the drag while saving
    retirement_tax_drag: dict = field(default_factory=lambda: {"equity": 0.003, "gold": 0.003, "debt": 0.009, "ppf": 0.0})
    inflation_general: float = 0.055
    inflation_sd: float = 0.018
    inflation_medical: float = 0.115
    inflation_education: float = 0.09
    medical_weight_in_retirement: float = 0.15
    equity_vol_scale: float | None = None          # None: derived from the 20-year / joint-window volatility ratio
    n_paths: int = 5000
    block_months: int = 6
    seed: int = 20261021

    def after_tax(self, asset: str, retired: bool = False) -> float:
        return getattr(self, asset) - (self.retirement_tax_drag if retired else self.tax_drag).get(asset, 0.0)

    @property
    def retirement_inflation(self) -> float:
        w = self.medical_weight_in_retirement
        return (1 - w) * self.inflation_general + w * self.inflation_medical


SCENARIOS = {"cautious": dict(equity=0.085, gold=0.055, debt=0.065),
             "base": {},
             "hopeful": dict(equity=0.12, gold=0.08, debt=0.075)}


def scenario(name: str, base: Assumptions | None = None) -> Assumptions:
    return replace(base or Assumptions(), **SCENARIOS[name])
