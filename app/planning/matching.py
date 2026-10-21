"""
Which goal each holding serves (docs/PLANNING.md §6), decided from market data and the goals' own numbers, not from a fixed fund-to-goal list.

Every holding has a measured profile (funds.py): the SEBI category AMFI lists it under, what its price history shows it holds (large / mid / small cap, gold,
debt), and the volatility that is its own. Every goal has a need today (the planner's), a date, and so a risk budget (risk.py): the portfolio it holds may
not need more years than the goal has. A holding goes to the goal that is furthest from funded among those whose budget it fits. Holdings are moved only when
their goal would want them under half as much as the best one; tags you set yourself are never touched; the matcher's are marked `auto`, so a sale, a
purchase, a fund changing what it holds, or the market's volatility moving changes the match on the next run (after each sync and daily), with nobody there.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import date

import numpy as np

from app.planning import funds, risk

logger = logging.getLogger(__name__)

STICK = 0.5                        # keep a holding where it is while its goal wants it at least this share as much as the best goal does
LOW_RISK_YEARS = 1.0               # a holding whose own 1-in-20 horizon is within this is cash-like: it may close the emergency fund's gap
PPF_YEARS = 5.0                    # PPF is locked for 15 years, with partial withdrawals after 5


@dataclass
class Goal:
    tag: str                       # the tag written for this goal
    tags: frozenset                # every tag that already counts for it (family retirement: retirement + wife_retirement)
    kind: str
    years: float
    pv_need: float                 # what must be held today to meet the need (0 or less: nothing to fund)
    scope: frozenset
    value: float = 0.0             # held now (fixed tags and cash)
    exposure: np.ndarray = field(default_factory=lambda: np.zeros(len(funds.FACTORS)))   # value-weighted factor exposure of what is held
    idio: float = 0.0              # sum of value² × unexplained variance

    @property
    def priority(self) -> bool:
        return self.kind in ("emergency", "emergency_fund")


@dataclass
class Item:
    key: str
    owner: str
    value: float
    profile: "funds.Profile"
    kind: str                      # mutual_fund | equity | bonds | manual | ppf
    current: str | None            # tag now
    managed: bool                  # untagged, or tagged by the matcher / the old fixed rule (not set by you)


def _take(goal: Goal, item: Item, sign: float = 1.0) -> None:
    goal.value += sign * item.value
    goal.exposure = goal.exposure + sign * item.value * item.profile.exposure
    goal.idio += sign * item.value ** 2 * item.profile.idio


def portfolio_horizon(model: "risk.RiskModel", goal: Goal, extra: Item | None = None) -> float:
    """Years the goal's holdings (with `extra` added) need: the 1-in-20 horizon of that portfolio."""
    v, e, i = goal.value, goal.exposure, goal.idio
    if extra is not None:
        v, e, i = v + extra.value, e + extra.value * extra.profile.exposure, i + extra.value ** 2 * extra.profile.idio
    if v <= 0:
        return 0.0
    mu, sd = model.stats(e / v, i / v ** 2)
    return risk.horizon(mu, sd)


def eligible(item: Item, goal: Goal) -> bool:
    if item.owner not in goal.scope:
        return False
    if item.kind == "ppf" and (goal.years < PPF_YEARS or goal.priority):
        return False                                   # locked for years
    return True


def score(model: "risk.RiskModel", item: Item, goal: Goal) -> float:
    """How much the goal wants this holding: how far it is from funded, times how well the holding fits its risk budget (full when the portfolio still
    needs no more years than the goal has, shrinking to nothing as the portfolio would need twice as long)."""
    if goal.pv_need <= 0 or not eligible(item, goal):
        return 0.0
    deficit = max(0.0, goal.pv_need - goal.value) / goal.pv_need
    years = max(goal.years, risk.MIN_YEARS)
    h = portfolio_horizon(model, goal, item)
    feas = 1.0 if h <= years else max(0.0, 1.0 - (h - years) / years)
    return deficit * feas


def standalone_horizon(model: "risk.RiskModel", item: Item) -> float:
    mu, sd = model.stats(item.profile.exposure, item.profile.idio)
    return risk.horizon(mu, sd)


def assign(model: "risk.RiskModel", items: list[Item], goals: list[Goal]) -> dict[str, str | None]:
    """{item key: tag} for every managed item (None when nothing fits: it stays untagged). Deterministic: biggest holdings first.

    Every managed holding starts in the goal it carries now, so the picture is the real one; each is then offered to all goals with its own weight
    taken out of its current goal, and it moves only when another goal wants it clearly more (`STICK`). Untagged cash-like holdings first close the
    emergency fund's gap; everything else untagged goes to the goal that wants it most."""
    out: dict[str, str | None] = {}
    todo = sorted((i for i in items if i.managed), key=lambda i: (-i.value, i.key))
    now: dict[str, Goal | None] = {}
    for it in todo:
        g = next((g for g in goals if it.current in g.tags and it.owner in g.scope), None) if it.current else None
        now[it.key] = g
        if g is not None:
            _take(g, it)
    for g in goals:                                                    # the emergency fund first
        if not g.priority:
            continue
        for it in todo:
            if now[it.key] is None and standalone_horizon(model, it) <= LOW_RISK_YEARS and eligible(it, g) and g.pv_need - g.value > 0.5 * it.value:
                _take(g, it)
                now[it.key] = g
                out[it.key] = g.tag
    for it in todo:
        cur = now[it.key]
        if it.key in out:
            continue
        if cur is not None:
            _take(cur, it, -1.0)
            if cur.priority and standalone_horizon(model, it) <= LOW_RISK_YEARS and cur.pv_need - cur.value > 0.5 * it.value:
                _take(cur, it)                                          # the emergency fund still needs it
                out[it.key] = it.current if it.current in cur.tags else cur.tag
                continue
        scores = {g.tag: score(model, it, g) for g in goals}
        scores = {t: v for t, v in scores.items() if v > 0}
        if not scores:
            pick = cur                                              # nothing wants it better: it stays where it is (or untagged)
        else:
            best = max(scores, key=lambda t: (scores[t], t))
            pick = cur if (cur is not None and scores.get(cur.tag, 0.0) >= STICK * scores[best]) else next(g for g in goals if g.tag == best)
        if pick is not None:
            _take(pick, it)
        out[it.key] = (it.current if (pick is not None and it.current in pick.tags) else (pick.tag if pick else None))
    return out


# ── database side ──────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────


def inherit_tags(db) -> int:
    """A new snapshot row of a holding already tagged (same owner, same ISIN) takes that tag and its source instead of being matched again."""
    from sqlalchemy import text
    res = db.execute(text("""
        UPDATE holdings h SET goal_tag = s.tag, goal_tag_source = s.src
        FROM (SELECT DISTINCT ON (a.owner_id, x.isin) a.owner_id, x.isin, x.goal_tag AS tag, x.goal_tag_source AS src
              FROM holdings x JOIN accounts a ON a.id = x.account_id
              WHERE x.goal_tag IS NOT NULL AND x.isin IS NOT NULL
              ORDER BY a.owner_id, x.isin, x.snapshot_date DESC) s, accounts ha
        WHERE h.goal_tag IS NULL AND ha.id = h.account_id AND ha.owner_id = s.owner_id AND h.isin = s.isin"""))
    return res.rowcount or 0


def _risk_of(db, owners: list[str]) -> str:
    from app.models.schemas import Owner
    risks = {(o.risk_appetite or "moderate").lower() for o in db.query(Owner).filter(Owner.id.in_(owners)).all()}
    return risks.pop() if len(risks) == 1 else "moderate"


def _profile_for(h_isin: str, name: str | None, symbol: str | None, htype: str | None, cat: dict, factors, today: date) -> "funds.Profile":
    """One holding's measured profile; every data source degrades to the next (own history → SEBI category → name)."""
    key = h_isin
    if h_isin.startswith("INF"):
        c = cat.get(h_isin)
        rets = funds.fund_returns(c["code"], today) if c else None
        held = None
        try:                                                                 # the fund's own disclosed portfolio, when it can be had
            held = funds.holdings_exposure(funds.groww_scheme(h_isin, (c or {}).get("name") or name), funds.amfi_caps())
        except Exception as exc:
            logger.warning("holdings for %s unavailable: %s", h_isin, exc)
        return funds.fund_profile(key, rets, factors, c["category"] if c else None, name or (c or {}).get("name"), held)
    if h_isin.startswith("IN0"):                                            # bonds: sovereign gold bonds or government / corporate debt
        return funds.fixed_profile(key, "gold" if re.search(r"sgb|gold", name or "", re.I) else "debt")
    info = funds.stock_symbol(h_isin) or {}
    rets = funds.yahoo_returns(info["symbol"], today) if info.get("symbol") else None
    return funds.stock_profile(key, rets, factors, info.get("sector"))


def run(db, owner_id: str | None = None, apply: bool = True, only_untagged: bool = False, today: date | None = None, commit: bool = True) -> dict:
    """Match holdings (and manual assets such as FDs and SGBs) to goals. `only_untagged`: fill gaps only (after a sync); otherwise every
    holding the matcher manages is re-checked. `apply=False` returns the moves without writing them. Nothing changes when market data is unavailable."""
    from app.engine.goal_returns import goal_tags
    from app.engine.goals import _scope, project_all_goals
    from app.engine.holding_tagger import suggest_goal_tag
    from app.engine.holdings_view import latest_holdings
    from app.engine.networth import real_owners
    from app.models.schemas import Account, Goal as GoalRow, Holding, ManualAsset
    from app.planning import goal_plan

    today = today or date.today()
    inherit_tags(db)
    ctx = goal_plan.get_context(db)
    if ctx is None or ctx.risk_model is None:
        raise RuntimeError("market data (fund and index histories) is unavailable")
    model, factors, cat = ctx.risk_model, ctx.factors, funds.catalog()
    owners = [o.id for o in real_owners(db)]
    rows = latest_holdings(db, owner_ids=[owner_id] if owner_id and owner_id != "family" else owners)
    proj = {str(p.goal_id): p for p in project_all_goals(db)}
    goal_rows = [g for g in db.query(GoalRow).all() if (g.goal_type or "").lower() not in ("custom", "") and str(g.id) in proj]

    items: list[Item] = []
    meta: dict[str, dict] = {}
    for h in rows:
        if not h.isin or float(h.current_value or 0) <= 0:
            continue
        legacy = suggest_goal_tag(h.isin, h.instrument_name, h.holding_type)
        src = getattr(h, "goal_tag_source", None)
        managed = h.goal_tag is None or src == "auto" or (src is None and h.goal_tag == legacy)
        if only_untagged and h.goal_tag is not None:
            managed = False
        key = f"h:{h.owner_id}:{h.isin}"
        prof = _profile_for(h.isin, h.instrument_name, h.symbol, h.holding_type, cat, factors, today)
        items.append(Item(key, h.owner_id, float(h.current_value), prof, (h.holding_type or "").lower(), h.goal_tag, managed))
        meta[key] = {"name": h.instrument_name, "src": src, "profile": prof.summary()}
    for m in db.query(ManualAsset).filter(ManualAsset.owner_id.in_(owners)).all():
        if m.is_liquid or float(m.current_value or 0) <= 0 or (m.asset_type or "").lower() in ("real_estate", "liability", "other", "insurance"):
            continue                                                  # a liquid FD already counts for the emergency fund; a home is not a corpus
        managed = m.goal_tag is None or getattr(m, "goal_tag_source", None) == "auto"
        if only_untagged and m.goal_tag is not None:
            managed = False
        at = (m.asset_type or "").lower()
        kind_key = "gold" if at in ("gold", "sgb") else "silver" if at == "silver" else "nps" if at == "nps" else "debt"
        key = f"m:{m.id}"
        prof = funds.fixed_profile(key, kind_key)
        items.append(Item(key, m.owner_id, float(m.current_value), prof, "ppf" if at == "ppf" else "manual", m.goal_tag, managed))
        meta[key] = {"name": m.name, "src": getattr(m, "goal_tag_source", None), "profile": prof.summary()}

    goals: list[Goal] = []
    for g in goal_rows:
        p = proj[str(g.id)]
        tags = frozenset(goal_tags(g))
        scope = frozenset(_scope(g, owners))
        kind = (g.goal_type or "").lower()
        years = max(0.0, ((p.target_date or today) - today).days / 365.25)
        r = (p.effective_return_pct or 8.0) / 100
        emergency = kind in ("emergency", "emergency_fund")
        need = float(p.inflation_adj_target or 0) if emergency else float(p.inflation_adj_target or 0) / (1 + r) ** years
        goal = Goal(goal_tags(g)[0], tags, kind, years, need, scope)
        itemised = 0.0
        for i in items:
            if i.current in tags and i.owner in scope and not i.managed:
                _take(goal, i)
                itemised += i.value
        managed_now = sum(i.value for i in items if i.managed and i.current in tags and i.owner in scope)
        rest = max(0.0, float(p.current_corpus or 0) - itemised - managed_now)          # cash and anything else the goal's own corpus counts (assign() adds the managed holdings back)
        goal.value += rest
        goal.exposure = goal.exposure + rest * np.eye(len(funds.FACTORS))[funds.FACTORS.index("debt")]
        goals.append(goal)
    result = assign(model, items, goals)

    moves = []
    for it in items:
        if it.key in result and result[it.key] != it.current:
            moves.append({"key": it.key, "name": meta[it.key]["name"], "owner": it.owner, "value": round(it.value), "from": it.current, "to": result[it.key],
                          "profile": meta[it.key]["profile"]})
    if apply:
        for it in items:
            if it.key not in result or (result[it.key] == it.current and meta[it.key]["src"] == "auto"):
                continue
            new = result[it.key]
            if it.key.startswith("h:"):
                _, owner, isin = it.key.split(":", 2)
                accts = [a for (a,) in db.query(Account.id).filter(Account.owner_id == owner).all()]
                db.query(Holding).filter(Holding.isin == isin, Holding.account_id.in_(accts)).update(
                    {"goal_tag": new, "goal_tag_source": "auto" if new else None}, synchronize_session=False)
            else:
                m = db.get(ManualAsset, it.key[2:])
                m.goal_tag, m.goal_tag_source = new, ("auto" if new else None)
        db.commit() if commit else db.flush()
    return {"moves": moves, "managed": sum(1 for i in items if i.managed), "fixed": sum(1 for i in items if not i.managed),
            "goals": [{"tag": g.tag, "years": round(g.years, 1), "need_today": round(g.pv_need), "held": round(g.value),
                       "needs_years": round(portfolio_horizon(model, g), 1)} for g in goals],
            "holdings": {meta[i.key]["name"]: meta[i.key]["profile"] for i in items}, "applied": apply}


def speculative(db, limit_years: float = 40.0, min_value: float = 50000.0, today: date | None = None) -> list[dict]:
    """Holdings whose own risk needs more years than any goal has (the 1-in-20 horizon beyond `limit_years`): money that is a bet, not goal money.
    Silver is the usual one (about 35% a year of volatility and no premium over gold in the planner's expectations)."""
    from app.engine.holdings_view import latest_holdings
    from app.engine.networth import real_owners
    from app.planning import goal_plan
    today = today or date.today()
    ctx = goal_plan.get_context(db)
    if ctx is None or ctx.risk_model is None:
        return []
    cat = funds.catalog()
    out = []
    for h in latest_holdings(db, owner_ids=[o.id for o in real_owners(db)]):
        v = float(h.current_value or 0)
        if not h.isin or v < min_value:
            continue
        prof = _profile_for(h.isin, h.instrument_name, h.symbol, h.holding_type, cat, ctx.factors, today)
        mu, sd = ctx.risk_model.stats(prof.exposure, prof.idio)
        hz = risk.horizon(mu, sd)
        if hz > limit_years and not (h.isin.startswith("INE") and hz < 60):
            out.append({"name": h.instrument_name, "owner": h.owner_id, "value": v, "needs_years": round(hz), "volatility_pct": round(sd * 100), "category": prof.category})
    return sorted(out, key=lambda r: -r["value"])
