"""
Effective-dated public rules (rules.yml): one place for limits, rates and thresholds that Parliament, the RBI
or the Government change. Code asks `value("ppf.annual_limit")` (today) or `value(id, on=some_date)`.

An entry is in force from its `effective_from` until the day before the next entry with the same id. Asking for a
date before the first entry raises RuleNotInForce: the registry does not guess history it has not verified.
Only the rules are public; nothing about the family's money lives here.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from functools import lru_cache
from pathlib import Path

import yaml

PATH = Path(__file__).with_name("rules.yml")
STALE_AFTER_DAYS = 120


class RuleNotInForce(LookupError):
    """No verified entry covers the requested date (or the id does not exist)."""


@dataclass(frozen=True)
class Rule:
    id: str
    value: Decimal
    unit: str
    effective_from: date
    source: str
    verified_on: date
    note: str = ""


@lru_cache(maxsize=1)
def load(path: Path = PATH) -> dict[str, tuple[Rule, ...]]:
    """id → its entries, oldest first. Raises on a malformed file (the tests load it too)."""
    raw = yaml.safe_load(path.read_text())["rules"]
    out: dict[str, list[Rule]] = {}
    for e in raw:
        r = Rule(id=e["id"], value=Decimal(str(e["value"])), unit=e["unit"], effective_from=e["effective_from"],
                 source=e["source"], verified_on=e["verified_on"], note=e.get("note", ""))
        out.setdefault(r.id, []).append(r)
    return {k: tuple(sorted(v, key=lambda r: r.effective_from)) for k, v in out.items()}


def rule(rule_id: str, on: date | None = None) -> Rule:
    entries = load().get(rule_id)
    if not entries:
        raise RuleNotInForce(f"unknown rule {rule_id!r}")
    when = on or date.today()
    in_force = [r for r in entries if r.effective_from <= when]
    if not in_force:
        raise RuleNotInForce(f"{rule_id} has no verified entry before {entries[0].effective_from} (asked for {when})")
    return in_force[-1]


def value(rule_id: str, on: date | None = None) -> Decimal:
    return rule(rule_id, on).value


def _last_quarter_start(today: date) -> date:
    return date(today.year, 3 * ((today.month - 1) // 3) + 1, 1)


def stale(today: date | None = None) -> list[dict]:
    """Rules in force today that need re-reading at their source: last verified over STALE_AFTER_DAYS ago, or
    verified before the most recent quarter start (PPF/small-savings and most rates are re-notified then)."""
    today = today or date.today()
    quarter = _last_quarter_start(today)
    out = []
    for rule_id in sorted(load()):
        r = rule(rule_id, today)
        age = (today - r.verified_on).days
        if age > STALE_AFTER_DAYS or r.verified_on < quarter:
            out.append({"id": r.id, "verified_on": r.verified_on.isoformat(), "age_days": age, "source": r.source})
    return out
