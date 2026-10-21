"""
Which goal each holding serves (docs/PLANNING.md §6): decided from the goals' own horizons and funding gaps, not from a fixed fund-to-goal list.

A holding is matched to the goal that wants it most: how far the goal is from being funded (its need today, from the planner) times how well the holding
fits the mix the goal should hold at its horizon (long goals take equity, near goals debt, the emergency fund only debt; a direct stock needs 10+ years, PPF 5+).
A holding is moved only when its goal no longer fits well. Tags you set yourself are never touched; every tag the matcher writes is marked `auto`, so selling,
buying or a goal's date moving changes the match on the next run (daily, and after each sync) with nobody there.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date

from app.planning import allocation

logger = logging.getLogger(__name__)

STICK = 0.5                        # keep a holding where it is while its goal wants it at least this share as much as the best goal does
MIN_FIT = 0.2                      # a goal whose buckets are almost full for what the holding is made of is not a match
DIRECT_STOCK_YEARS = 10.0          # a single company's shares only for goals this far away
PPF_YEARS = 5.0                    # PPF is locked for 15 years, with partial withdrawals after 5


@dataclass
class Goal:
    tag: str                       # the tag written for this goal
    tags: frozenset                # every tag that already counts for it (family retirement: retirement + wife_retirement)
    kind: str
    years: float
    pv_need: float                 # what must be held today to meet the need (0 or less: nothing to fund)
    held: dict                     # value already counted toward it, by asset class, from tags the matcher does not manage
    scope: frozenset
    mix: dict = field(default_factory=dict)

    @property
    def priority(self) -> bool:
        return self.kind in ("emergency", "emergency_fund")


@dataclass
class Item:
    key: str
    owner: str
    value: float
    weights: dict                  # equity / gold / debt / ppf (allocation.classify)
    kind: str                      # mutual_fund | equity | bonds | manual
    current: str | None            # tag now
    managed: bool                  # untagged, or tagged by the matcher / the old fixed rule (not set by you)


def goal_mix(years: float, kind: str, risk: str = "moderate") -> dict:
    return allocation.split_for(allocation.recommended_equity(max(0.0, years), risk, kind), kind)


def classes(item: Item) -> dict:
    """The holding's make-up over equity / gold / debt (PPF counts as debt)."""
    w = dict(item.weights)
    if "ppf" in w:
        w["debt"] = w.get("debt", 0.0) + w.pop("ppf")
    return w


def eligible(item: Item, goal: Goal) -> bool:
    if item.owner not in goal.scope:
        return False
    if item.kind == "equity" and goal.years < DIRECT_STOCK_YEARS:
        return False                                   # one company's shares are too lumpy for a near goal
    if "ppf" in item.weights and (goal.years < PPF_YEARS or goal.priority):
        return False                                   # locked
    return True


def room(goal: Goal, c: str) -> tuple[float, float]:
    """(still to hold, target) in asset class c: the goal's need times the mix it should have at its horizon, less what it holds."""
    target = goal.pv_need * goal.mix.get(c, 0.0)
    return max(0.0, target - goal.held.get(c, 0.0)), target


def score(item: Item, goal: Goal) -> float:
    """How much the goal wants this holding: for each class it is made of, how empty that class's bucket is, and how much of the holding fits into it."""
    if goal.pv_need <= 0 or not eligible(item, goal):
        return 0.0
    s = 0.0
    for c, w in classes(item).items():
        r, t = room(goal, c)
        if t > 0 and w > 0:
            s += w * (r / t) * min(1.0, r / (w * item.value))
    return s if s >= MIN_FIT * 0.5 else 0.0


def _take(goal: Goal, item: Item, sign: float = 1.0) -> None:
    for c, w in classes(item).items():
        goal.held[c] = goal.held.get(c, 0.0) + sign * w * item.value


def assign(items: list[Item], goals: list[Goal]) -> dict[str, str | None]:
    """{item key: tag} for every managed item (None when nothing fits: it stays untagged). Deterministic: biggest holdings first.

    Every managed holding starts in the goal it carries now, so the picture is the real one; each is then offered to all goals with its own
    weight taken out of its current goal, and it moves only when another goal wants it clearly more (`STICK`). Untagged debt holdings first close the
    emergency fund's gap; everything else untagged goes to the goal that wants it most."""
    out: dict[str, str | None] = {}
    todo = sorted((i for i in items if i.managed), key=lambda i: (-i.value, i.key))
    now: dict[str, Goal | None] = {}
    for it in todo:
        g = next((g for g in goals if it.current in g.tags and it.owner in g.scope), None) if it.current else None
        now[it.key] = g
        if g is not None:
            _take(g, it)
    for g in goals:                                                    # the emergency fund first: untagged debt holdings until the gap is closed
        if not g.priority:
            continue
        for it in todo:
            if now[it.key] is None and classes(it).get("debt", 0.0) >= 0.9 and eligible(it, g) and g.pv_need - sum(g.held.values()) > 0.5 * it.value:
                _take(g, it)
                now[it.key] = g
                out[it.key] = g.tag
    for it in todo:
        cur = now[it.key]
        if it.key in out:
            continue
        if cur is not None:
            _take(cur, it, -1.0)
            if cur.priority and classes(it).get("debt", 0.0) >= 0.9 and cur.pv_need - sum(cur.held.values()) > 0.5 * it.value:
                _take(cur, it)                                          # the emergency fund still needs it
                out[it.key] = it.current if it.current in cur.tags else cur.tag
                continue
        scores = {g.tag: score(it, g) for g in goals}
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


def run(db, owner_id: str | None = None, apply: bool = True, only_untagged: bool = False, today: date | None = None, commit: bool = True) -> dict:
    """Match holdings (and manual assets such as FDs and SGBs) to goals. `only_untagged`: fill gaps only (after a sync); otherwise every
    holding the matcher manages is re-checked. `apply=False` returns the moves without writing them."""
    from app.engine.goal_returns import goal_tags
    from app.engine.goals import _scope, project_all_goals
    from app.engine.holding_tagger import suggest_goal_tag
    from app.engine.holdings_view import latest_holdings
    from app.engine.networth import real_owners
    from app.models.schemas import Account, Goal as GoalRow, Holding, ManualAsset

    today = today or date.today()
    inherit_tags(db)
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
        items.append(Item(key, h.owner_id, float(h.current_value), allocation.classify(h.instrument_name, h.holding_type), (h.holding_type or "").lower(), h.goal_tag, managed))
        meta[key] = {"name": h.instrument_name, "src": src}
    for m in db.query(ManualAsset).filter(ManualAsset.owner_id.in_(owners)).all():
        if m.is_liquid or float(m.current_value or 0) <= 0 or (m.asset_type or "").lower() in ("real_estate", "liability", "other", "insurance"):
            continue                                                  # a liquid FD already counts for the emergency fund; a home is not a corpus
        managed = m.goal_tag is None or getattr(m, "goal_tag_source", None) == "auto"
        if only_untagged and m.goal_tag is not None:
            managed = False
        key = f"m:{m.id}"
        items.append(Item(key, m.owner_id, float(m.current_value), allocation.classify(m.name, None, m.asset_type), "manual", m.goal_tag, managed))
        meta[key] = {"name": m.name, "src": getattr(m, "goal_tag_source", None)}

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
        mix = goal_mix(years, kind, _risk_of(db, list(scope)))
        held: dict[str, float] = {}
        itemised = 0.0
        for i in items:
            if i.current in tags and i.owner in scope and not i.managed:
                for c, w in classes(i).items():
                    held[c] = held.get(c, 0.0) + w * i.value
                itemised += i.value
        managed_now = sum(i.value for i in items if i.managed and i.current in tags and i.owner in scope)
        rest = max(0.0, float(p.current_corpus or 0) - itemised - managed_now)          # cash and anything else the goal's own corpus counts (assign() adds the managed holdings back)
        for c, share in ((("debt", 1.0),) if emergency else tuple(mix.items())):
            held[c] = held.get(c, 0.0) + rest * share
        goals.append(Goal(goal_tags(g)[0], tags, kind, years, need, held, scope, mix))
    result = assign(items, goals)

    moves = []
    for it in items:
        if it.key in result and result[it.key] != it.current:
            moves.append({"key": it.key, "name": meta[it.key]["name"], "owner": it.owner, "value": round(it.value), "from": it.current, "to": result[it.key]})
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
            "goals": [{"tag": g.tag, "years": round(g.years, 1), "need_today": round(g.pv_need), "funded": round(sum(g.held.values())),
                       "mix": {k: round(v, 2) for k, v in g.mix.items()}} for g in goals], "applied": apply}
