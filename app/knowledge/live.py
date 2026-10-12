"""
Keeps selected public rules current by reading their official page (today: the RBI repo rate from rbi.org.in).

Each run stores what the page says as a RuleObservation; app.knowledge.rules lays the observations over rules.yml.
Nothing is written unless the page was reachable and parsed to a plausible number, so an outage or a redesigned page
leaves the last known value in place (and the staleness warning says so once it is old). Rules without an official
machine-readable page (tax thresholds, PPF limit, EPF rate) stay hand-verified in rules.yml.
"""
from __future__ import annotations

import logging
import re
from datetime import date
from decimal import Decimal

import httpx
from sqlalchemy.orm import Session

from app.knowledge import rules
from app.models.schemas import RuleObservation

logger = logging.getLogger(__name__)

RBI_HOME = "https://www.rbi.org.in/"
_REPO = re.compile(r"Repo Rate\s*:\s*(\d{1,2}(?:\.\d{1,2})?)\s*%", re.I)


def _n(d: Decimal) -> str:
    return format(d.normalize(), "f")


def parse_repo(html: str) -> Decimal | None:
    """The 'Policy Repo Rate : 5.50%' line of the RBI home page's current-rates box; None if absent or implausible."""
    text = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html).replace("&nbsp;", " "))
    m = _REPO.search(text)
    if not m:
        return None
    v = Decimal(m.group(1))
    return v if 0 < v <= 15 else None


def fetch_repo() -> Decimal | None:
    r = httpx.get(RBI_HOME, timeout=25, follow_redirects=True, headers={"User-Agent": "Mozilla/5.0 (kutumbh rules refresh)"})
    r.raise_for_status()
    return parse_repo(r.text)


# rule id → (fetcher, the page it reads)
FETCHERS = {"rbi.repo_rate": (fetch_repo, RBI_HOME)}
PROBE_MAX_AGE_DAYS = 10    # a rule whose probe has not matched for this long is reported: the page changed or went away
MAX_AGE_DAYS = 3          # a self-updating rule not read again within this many days is reported (the daily job missed 3 runs)


def page_text(url: str) -> str:
    """The visible text of a page. NSI serves its own certificate without the intermediate, so it uses the bundled chain."""
    verify: object = True
    if "nsiindia.gov.in" in url:
        from app.engine.small_savings import _ssl_context
        verify = _ssl_context()
    r = httpx.get(url, timeout=30, follow_redirects=True, verify=verify,
                  headers={"User-Agent": "Mozilla/5.0 (kutumbh rules check)"})
    r.raise_for_status()
    return re.sub(r"\s+", " ", re.sub(r"<(style|script).*?</\1>", " ", re.sub(r"<[^>]+>", " ", r.text), flags=re.S | re.I))


def probed_rule_ids() -> list[str]:
    """Rules whose entry in force carries a probe: re-confirmed automatically."""
    out = []
    for rid in rules.load():
        r = rules.rule(rid)
        if r.probe:
            out.append(rid)
    return sorted(out)


def verify_sources(db: Session, fetch=None, today: date | None = None) -> dict[str, str]:
    """Re-read the page behind every probed rule. A page that still contains its probe confirms the rule's value, which is recorded
    as an observation equal to the file's value (so `verified_on` moves to today with no human). A page that does not match, or
    cannot be read, writes nothing: the rule then warns once PROBE_MAX_AGE_DAYS pass."""
    fetch = fetch or page_text
    today = today or date.today()
    pages: dict[str, str | None] = {}
    out: dict[str, str] = {}
    for rid in probed_rule_ids():
        r = rules.rule(rid, today)
        url = r.probe_url or r.source
        if url not in pages:
            try:
                pages[url] = fetch(url)
            except Exception as exc:
                logger.warning("probe for %s: %s unreadable: %s", rid, url, exc)
                pages[url] = None
        text = pages[url]
        if text is None:
            out[rid] = "unreachable"
        elif re.search(r.probe, text, re.I):
            _record(db, rid, r.value, url, today)
            out[rid] = "confirmed"
        else:
            logger.warning("probe for %s no longer matches %s", rid, url)
            out[rid] = "not confirmed"
    db.commit()
    load_observations(db)
    return out


def _record(db: Session, rule_id: str, value: Decimal, source: str, today: date) -> None:
    last = (db.query(RuleObservation).filter(RuleObservation.rule_id == rule_id)
            .order_by(RuleObservation.first_seen.desc(), RuleObservation.last_seen.desc()).first())
    if last is not None and last.value == value:
        last.last_seen = max(last.last_seen, today)
    else:
        db.add(RuleObservation(rule_id=rule_id, value=value, first_seen=today, last_seen=today, source=source[:300]))


def load_observations(db: Session) -> int:
    """Lay the stored observations over the registry (run at start-up and after every refresh)."""
    rows = db.query(RuleObservation).order_by(RuleObservation.first_seen).all()
    rules.set_observations([(r.rule_id, r.value, r.first_seen, r.last_seen, r.source) for r in rows])
    return len(rows)


def refresh(db: Session, fetchers: dict | None = None, today: date | None = None) -> dict[str, str]:
    """Read each page once. Returns {rule id: what happened} (unchanged | changed 5.50 → 5.75 | unavailable | unreadable)."""
    today = today or date.today()
    out: dict[str, str] = {}
    for rule_id, (fetch, source) in (FETCHERS if fetchers is None else fetchers).items():
        try:
            value = fetch()
        except Exception as exc:                                   # offline, blocked, TLS: keep what we have
            logger.warning("live rule %s: page unavailable: %s", rule_id, exc)
            out[rule_id] = "unavailable"
            continue
        if value is None:
            logger.warning("live rule %s: page read but the value was not found; left unchanged", rule_id)
            out[rule_id] = "unreadable"
            continue
        last = (db.query(RuleObservation).filter(RuleObservation.rule_id == rule_id)
                .order_by(RuleObservation.first_seen.desc(), RuleObservation.last_seen.desc()).first())
        if last is not None and last.value == value:
            last.last_seen = max(last.last_seen, today)
            out[rule_id] = "unchanged"
            continue
        before = last.value if last is not None else rules.value(rule_id, today)
        db.add(RuleObservation(rule_id=rule_id, value=value, first_seen=today, last_seen=today, source=source))
        out[rule_id] = "unchanged" if before == value else f"changed {_n(before)} → {_n(value)}"
    db.commit()
    load_observations(db)
    return out
