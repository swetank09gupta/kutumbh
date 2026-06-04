"""
AMFI ISIN lookup for Indian mutual funds.

Pulls the daily NAV master file from AMFI and exposes lookup_isin(scheme_name).
Used to fill ISIN on Zerodha Coin transactions (Coin emails carry the fund
name + folio but no ISIN), closing the per-instrument XIRR gap in
app.engine.portfolio_xirr.

NAVAll.txt format (semicolon-separated):
    Scheme Code;ISIN Div Payout/ISIN Growth;ISIN Div Reinvestment;Scheme Name;NAV;Date

Cache: data/amfi_nav_cache.json, refreshed every 24h.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

_AMFI_URL = "https://www.amfiindia.com/spages/NAVAll.txt"
_CACHE_FILE = Path("data/amfi_nav_cache.json")
_CACHE_HOURS = 24

# Tokens that don't help disambiguate one fund from another — stripped during
# name normalization so "Mirae Asset ELSS Tax Saver Fund - Direct Growth" and
# "Mirae Asset ELSS Tax Saver - Regular Growth" both collapse to the same key
# (but the Direct vs Regular split is preserved via the _PLAN_TOKEN below).
_NOISE = {
    "FUND", "SCHEME", "PLAN", "OPTION", "PAYOUT", "REINVESTMENT",
    "PROGRAMME", "THE",
}
# IDCW and BONUS are plan-variants — not noise. They distinguish ISINs for the
# same fund (e.g. SBI Tech "Growth" ISIN vs "IDCW Payout" ISIN). Handled via
# the suffix marker in _normalize() below.

# Fund-category tokens that MUST match exactly when present — collapsing them
# turns a Nippon Floater Fund into a Nippon Growth Fund. If AMFI's name carries
# any of these and the query does not (or vice versa), the candidate is rejected
# even with high token overlap.
_CATEGORY_TOKENS = {
    "FLOATER", "LIQUID", "GILT", "DEBT", "HYBRID", "BALANCED",
    "ARBITRAGE", "AGGRESSIVE", "CONSERVATIVE", "BOND", "INCOME",
    "OVERNIGHT", "ULTRA", "SHORT", "DYNAMIC", "CORPORATE",
    "BANKING", "PSU", "CREDIT", "RISK", "FOCUSED", "CONTRA",
    "VALUE", "DIVIDEND", "YIELD", "EQUITY", "INDEX", "ETF",
    "SAVINGS", "MONEY", "MARKET", "TREASURY",
}

# Strip AMC suffix tokens that AMFI includes but Coin emails don't (e.g.
# "MIRAE ASSET MUTUAL FUND" → drop "MUTUAL FUND" tail).
_AMC_SUFFIX_RE = re.compile(r'\b(MUTUAL\s+FUND|MF|AMC)\b', re.IGNORECASE)


# AMFI spells IDCW as "Income Distribution cum Capital Withdrawal Option"
# (and variants). Collapse to IDCW before tokenizing — otherwise "INCOME" gets
# picked up as a category token and category-match rejects the legitimate pair.
_IDCW_LONGFORM_RE = re.compile(
    r'\bIncome\s+Distribution\s+cum\s+Capital\s+Withdrawal(\s+Option)?\b',
    re.IGNORECASE,
)


def _normalize(name: str) -> str:
    """Canonical token sequence for fund-name matching.

    Plan (Direct/Regular) and growth/IDCW are preserved as suffix markers so
    they survive noise stripping — they're the actual differentiator between
    two ISINs of the same scheme.
    """
    if not name:
        return ""
    # Collapse AMFI's "Income Distribution cum Capital Withdrawal" → IDCW
    # before uppercasing (regex is case-insensitive).
    txt = _IDCW_LONGFORM_RE.sub(' IDCW ', name).upper()
    txt = _AMC_SUFFIX_RE.sub(' ', txt)
    # Capture plan + variant before noise stripping
    is_direct = "DIRECT" in txt
    is_regular = "REGULAR" in txt
    is_growth = "GROWTH" in txt
    is_idcw = "IDCW" in txt or "DIVIDEND" in txt
    # Drop punctuation
    txt = re.sub(r'[^A-Z0-9]+', ' ', txt)
    parts = [p for p in txt.split() if p and p not in _NOISE
             and p not in ("DIRECT", "REGULAR", "GROWTH", "IDCW")]
    base = ' '.join(parts)
    suffix = []
    if is_direct:  suffix.append("DIR")
    elif is_regular: suffix.append("REG")
    if is_growth:  suffix.append("GR")
    elif is_idcw:  suffix.append("IDCW")
    return (base + (" " + " ".join(suffix) if suffix else "")).strip()


def _load_cache() -> Optional[dict]:
    if not _CACHE_FILE.exists():
        return None
    try:
        data = json.loads(_CACHE_FILE.read_text())
        ts = datetime.fromisoformat(data.get("fetched_at", ""))
        age_hr = (datetime.utcnow() - ts).total_seconds() / 3600
        if age_hr < _CACHE_HOURS:
            return data
    except Exception as exc:
        logger.debug("AMFI cache unreadable: %s", exc)
    return None


def _save_cache(entries: list[dict]) -> None:
    try:
        _CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        _CACHE_FILE.write_text(json.dumps({
            "fetched_at": datetime.utcnow().isoformat(),
            "entries":    entries,
        }))
    except Exception as exc:
        logger.warning("AMFI cache write failed: %s", exc)


def _fetch_navall() -> list[dict]:
    """Returns list of {isin, scheme_name, scheme_code, nav} dicts from AMFI."""
    resp = httpx.get(_AMFI_URL, timeout=30, follow_redirects=True)
    resp.raise_for_status()
    out: list[dict] = []
    for line in resp.text.splitlines():
        parts = line.split(";")
        if len(parts) < 5:
            continue
        code, isin_growth, isin_reinv, name, nav = parts[0], parts[1], parts[2], parts[3], parts[4]
        # Header / blank / section heading rows
        if not code.strip().isdigit():
            continue
        # Prefer Growth ISIN; fall back to Reinvestment.
        isin = (isin_growth or "").strip() or (isin_reinv or "").strip()
        if not isin or not isin.startswith("INF"):
            continue
        try:
            nav_f = float(nav.strip())
        except (ValueError, AttributeError):
            nav_f = 0.0
        out.append({
            "isin":        isin,
            "scheme_name": name.strip(),
            "scheme_code": code.strip(),
            "nav":         nav_f,
        })
    return out


def _build_index(entries: list[dict]) -> dict[str, str]:
    """normalized name → ISIN. Later entries don't overwrite earlier ones."""
    idx: dict[str, str] = {}
    for e in entries:
        key = _normalize(e["scheme_name"])
        if key and key not in idx:
            idx[key] = e["isin"]
    return idx


# Aliases for funds that AMFI has since renamed but Coin emails (and existing
# DB rows) still carry under the old name. Maps OLD scheme name → NEW name as
# it appears in NAVAll.txt (use a substring that uniquely identifies the new).
_RENAME_HINTS: dict[str, str] = {
    "MIRAE ASSET TAX SAVER":               "MIRAE ASSET ELSS TAX SAVER",
    "HDFC TAX SAVER":                      "HDFC ELSS TAX SAVER",
    "ADITYA BIRLA SUN LIFE TAX RELIEF96":  "ADITYA BIRLA SUN LIFE ELSS TAX SAVER",
    "ADITYA BIRLA SUN LIFE TAX RELIEF 96": "ADITYA BIRLA SUN LIFE ELSS TAX SAVER",
    "SBI BLUE CHIP":                       "SBI LARGE CAP",
    "SBI BLUECHIP":                        "SBI LARGE CAP",
    "PGIM INDIA MIDCAP OPPORTUNITIES":     "PGIM INDIA MIDCAP",
    # "Quant Active" (SEBI multicap pre-2018) has no current AMFI counterpart —
    # leave unresolved rather than mis-attributing to Flexi Cap / ELSS / Large
    # Mid Cap (all plausible successors but not confirmed).
}


_INDEX_CACHE: Optional[dict[str, str]] = None


def _get_index() -> dict[str, str]:
    global _INDEX_CACHE
    if _INDEX_CACHE is not None:
        return _INDEX_CACHE
    cached = _load_cache()
    if cached and cached.get("entries"):
        _INDEX_CACHE = _build_index(cached["entries"])
        return _INDEX_CACHE
    try:
        entries = _fetch_navall()
        _save_cache(entries)
        logger.info("Fetched AMFI NAVAll: %d schemes", len(entries))
        _INDEX_CACHE = _build_index(entries)
    except Exception as exc:
        logger.warning("AMFI fetch failed: %s — lookup disabled", exc)
        _INDEX_CACHE = {}
    return _INDEX_CACHE


def _split_suffix(norm_key: str) -> tuple[set[str], str]:
    """Split a normalized key into (base_token_set, suffix_str).

    The trailing "DIR GR" / "REG IDCW" markers are returned as a single
    suffix string so two candidate keys with the same plan+variant compare
    on identifier tokens only.
    """
    parts = norm_key.split()
    suffix_tokens: list[str] = []
    while parts and parts[-1] in ("DIR", "REG", "GR", "IDCW"):
        suffix_tokens.insert(0, parts.pop())
    return set(parts), " ".join(suffix_tokens)


def lookup_isin(scheme_name: str) -> Optional[str]:
    """Best-effort ISIN for a fund name. Returns None if no confident match."""
    if not scheme_name:
        return None
    idx = _get_index()
    if not idx:
        return None

    # Apply known renames before normalization so old aliases land on the new key.
    name_upper = scheme_name.upper()
    for old, new in _RENAME_HINTS.items():
        if old in name_upper:
            name_upper = name_upper.replace(old, new)
            break

    key = _normalize(name_upper)
    if not key:
        return None
    if key in idx:
        return idx[key]

    # Token-set match: highest Jaccard overlap on base tokens, with an exact
    # match on plan+variant suffix required (else "DIR GR" funds would match
    # "DIR IDCW" siblings).
    q_tokens, q_suffix = _split_suffix(key)
    if not q_tokens:
        return None
    q_categories = q_tokens & _CATEGORY_TOKENS
    best_isin: Optional[str] = None
    best_score = 0.0
    for k, isin in idx.items():
        k_tokens, k_suffix = _split_suffix(k)
        if not k_tokens:
            continue
        if q_suffix and k_suffix and q_suffix != k_suffix:
            continue
        # Hard reject: category mismatch. "Nippon Growth" must NOT match
        # "Nippon Floater" — Floater is a category token absent in the query.
        k_categories = k_tokens & _CATEGORY_TOKENS
        if q_categories != k_categories:
            continue
        inter = len(q_tokens & k_tokens)
        if inter < 2:
            continue
        # Anchor on the first token (AMC identifier — "PGIM", "SBI", "MIRAE").
        # Prevents "QUANT ACTIVE" matching "QUANTUM ACTIVE BOND" or similar.
        q_first = key.split()[0]
        if q_first not in k_tokens:
            continue
        # Coverage of the smaller set — handles both directions of extra tokens
        # (Coin has "OPPORTUNITIES" extra vs AMFI's shorter name, or vice versa).
        score = inter / min(len(q_tokens), len(k_tokens))
        if score > best_score:
            best_score = score
            best_isin = isin
    if best_isin and best_score >= 0.75:
        return best_isin
    return None


def refresh() -> int:
    """Force-refresh the cache from AMFI. Returns number of schemes loaded."""
    global _INDEX_CACHE, _NAV_CACHE
    entries = _fetch_navall()
    _save_cache(entries)
    _INDEX_CACHE = _build_index(entries)
    _NAV_CACHE   = {e["isin"]: e["nav"] for e in entries if e["nav"] > 0}
    return len(entries)


_NAV_CACHE: Optional[dict[str, float]] = None


def _get_nav_map() -> dict[str, float]:
    """ISIN → today's NAV from cached AMFI data. Loads cache if needed."""
    global _NAV_CACHE
    if _NAV_CACHE is not None:
        return _NAV_CACHE
    cached = _load_cache()
    if cached and cached.get("entries"):
        _NAV_CACHE = {e["isin"]: e.get("nav", 0.0)
                      for e in cached["entries"] if e.get("nav", 0.0) > 0}
        return _NAV_CACHE
    # Force a fetch
    try:
        refresh()
    except Exception as exc:
        logger.warning("AMFI NAV fetch failed: %s", exc)
        _NAV_CACHE = {}
    return _NAV_CACHE or {}


def get_nav(isin: str) -> Optional[float]:
    """Today's NAV for an MF ISIN, or None if unknown."""
    if not isin:
        return None
    nav = _get_nav_map().get(isin)
    return nav if nav and nav > 0 else None
