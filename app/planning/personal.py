"""
The personal facts the planner reads from config/accounts.yml (private, all optional): EPF balance and contributions, current pay, family history,
a return-of-premium term plan, the funding order. Pure readers: nothing here touches the database or the network.
"""
from __future__ import annotations

from datetime import date
from typing import Any

from app.planning.macro import param


def config() -> dict:
    try:
        from app.config import settings
        return settings.get_accounts_config() or {}
    except Exception:
        return {}


def _d(v: Any) -> date | None:
    if isinstance(v, date):
        return v
    try:
        return date.fromisoformat(str(v))
    except (TypeError, ValueError):
        return None


def epf_entries(cfg: dict | None = None) -> list[dict]:
    """[{owner, as_of, balance, employee_monthly, employer_monthly}] with numbers as floats; entries without a date or owner are dropped."""
    out = []
    for e in (cfg if cfg is not None else config()).get("epf") or []:
        as_of = _d(e.get("as_of"))
        if e.get("owner") and as_of:
            out.append({"owner": e["owner"], "as_of": as_of, "balance": float(e.get("balance") or 0),
                        "employee_monthly": float(e.get("employee_monthly") or 0), "employer_monthly": float(e.get("employer_monthly") or 0)})
    return out


def income(owner: str, cfg: dict | None = None) -> dict | None:
    v = ((cfg if cfg is not None else config()).get("income") or {}).get(owner)
    if not v or not v.get("ctc_annual"):
        return None
    return {"ctc_annual": float(v["ctc_annual"]), "from": _d(v.get("from")), "employer": v.get("employer")}


def term_policies(cfg: dict | None = None) -> list[dict]:
    return [{"owner": t.get("owner"), "premium_term_years": int(t.get("premium_term_years") or 0), "return_age": int(t.get("return_age") or 0),
             "return_amount": float(t.get("return_amount") or 0)}
            for t in (cfg if cfg is not None else config()).get("term_insurance") or [] if t.get("owner")]


def goal_priority(cfg: dict | None = None) -> list[str] | None:
    p = (cfg if cfg is not None else config()).get("goal_priority")
    return [str(x) for x in p] if p else None


def family_shift_years(history: list[dict] | None) -> float:
    """Years to move a person's survival curve by, from how long their ancestors lived.

    Longevity runs in families, but weakly: the heritability of lifespan is about a quarter (Danish twin studies). So the shift is that share of how far
    the ancestors who lived PAST the population's expected age at death (the age a 60-year-old reaches on average, from the SRS table) outlived it,
    capped at a few years. Ancestors younger than that say nothing yet (a parent of 64 may still die early or live to 95); alive ancestors count as at least their age."""
    p = lambda k: float(param("longevity", k)["value"])
    ref, h, cap = p("reference_age"), p("heritability"), p("family_shift_cap_years")
    ages = []
    for r in history or []:
        a = r.get("died_at") if r.get("died_at") is not None else r.get("alive_at")
        if a is not None and float(a) > ref:
            ages.append(float(a))
    if not ages:
        return 0.0
    return max(-cap, min(cap, h * sum(a - ref for a in ages) / len(ages)))
