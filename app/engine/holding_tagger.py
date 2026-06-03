"""
Auto-tag MF/equity holdings with the financial goal they serve.

Priority:
  1. ISIN_GOAL_MAP  — verified per-fund mapping (research-backed, see comments)
  2. Pattern rules  — regex on instrument_name for future unknown ISINs
  3. None           — leave untagged; user can set manually in the Holdings UI

Tagging is idempotent: holdings with an existing goal_tag are never overwritten,
so manual overrides from the UI are always preserved.
"""
from __future__ import annotations

import re
import logging

logger = logging.getLogger(__name__)

# ── Per-ISIN map (source: Value Research / AMFI, May 2026) ────────────────────
# Each ISIN is mapped to the goal it serves best based on fund category and
# standard Indian financial planning conventions.
ISIN_GOAL_MAP: dict[str, str | None] = {

    # ── Health & Senior Care → Large Cap ELSS + Large Cap funds ──────────────
    # Large-cap stability; funds the parents' senior care + medical emergency corpus.
    "INF179K01YS4": "health",        # HDFC ELSS Tax Saver Fund (large-cap biased)
    "INF109K016L0": "health",        # ICICI Prudential Large Cap Fund

    # ── Wife's Retirement → Balanced ELSS ────────────────────────────────────
    # Balanced equity allocation with tax efficiency; wife's long-horizon corpus.
    "INF769K01DM9": "wife_retirement",  # Mirae Asset ELSS Tax Saver Fund
    "INF209K01UN8": "wife_retirement",  # Aditya Birla SL ELSS Tax Saver Fund
    "INF966L01986": "wife_retirement",  # Quant ELSS Tax Saver Fund

    # ── Child Higher Education → Flexi Cap + Mid Cap ─────────────────────────
    # 10–15 yr horizon; higher growth needed for education inflation.
    "INF247L01445": "child_education",  # Motilal Oswal Midcap Fund
    "INF663L01DV3": "child_education",  # PGIM India Midcap Opportunities Fund
    "INF204K01E54": "child_education",  # Nippon India Growth Mid Cap Fund
    "INF879O01027": "child_education",  # Parag Parikh Flexi Cap Fund

    # ── Child Marriage → Gold / SGB / Silver ─────────────────────────────────
    # Capital preservation + inflation hedge over 18–22 yr horizon.
    "IN0020210178": "child_marriage",   # GOI SGB 30898 2029
    "IN0020220110": "child_marriage",   # GOI SGB 32415 2030
    "IN0020230069": "child_marriage",   # GOI SGB 33076 2031
    "INF209KB19F6": "child_marriage",   # Aditya Birla Sun Life Silver ETF
    "INF179KC1981": "child_marriage",   # HDFC Gold ETF

    # ── Your Retirement → Flexi/Mid/Large Cap + direct equity ─────────────────
    # Long-horizon diversified equity for the primary retirement corpus.
    "INF247L01999": "retirement",    # Motilal Oswal Large & Midcap Fund
    "INE01QM01018": "retirement",    # eMudhra Limited (direct equity)
    "INE733E01010": "retirement",    # NTPC Limited (direct equity)
    "INE213A01029": "retirement",    # ONGC (direct equity)
    "INE081A01020": "retirement",    # Tata Steel (direct equity)

    # ── Other Large Cap / Index → retirement ─────────────────────────────────
    "INF200K01QX4": "retirement",    # SBI Blue Chip Fund
    "INF204K01XI3": "retirement",    # Nippon India Large Cap Fund
    "INF846K01S29": "retirement",    # Axis Nifty 100 Index Fund

    # ── Multi Cap → child_marriage ────────────────────────────────────────────
    "INF966L01614": "child_marriage",   # Quant Multi Cap Fund

    # ── Sectoral → None ──────────────────────────────────────────────────────
    "INF200K01RT0": None,            # SBI Technology Opportunities Fund
}

# ── Pattern rules for unknown ISINs (applied in order; first match wins) ──────
# Regex matches against instrument_name.upper(). Maps to goal or None.
_NAME_RULES: list[tuple[re.Pattern, str | None]] = [
    # SGB / Gold / Silver → child_marriage (capital preservation + inflation hedge)
    (re.compile(r"\bSGB\b|SOVEREIGN\s*GOLD|GOI\s*SGB|\bSILVER\s*ETF\b"
                r"|\bGOLD\s*ETF\b|\bGOLD\b|\bSILVER\b", re.I),           "child_marriage"),
    # ELSS / Tax Saver — tagged by sub-category below; generic fallback → health
    (re.compile(r"\bELSS\b|TAX\s*SAV|TAX\s*PLAN|ETSF|ELSTSAV", re.I),   "health"),
    # Large Cap / Index → retirement
    (re.compile(r"\bLARGE\s*CAP\b|NIFTY\s*50\b|SENSEX\b|NIFTY100|NF100"
                r"|BLUE\s*CHIP|BLUECHIP|\bINDEX\b|\bSNF\b|\bLCF\b", re.I), "retirement"),
    # Flexi Cap → child_education
    (re.compile(r"FLEXI\s*CAP|FLEXICAP|\bFCF\b", re.I),                   "child_education"),
    # Large & Mid Cap (before standalone Mid Cap) → retirement
    (re.compile(r"LARGE\s*&?\s*MID|L&?M\s*CAP|LGMD|L&MCF", re.I),        "retirement"),
    # Balanced / Hybrid → wife_retirement
    (re.compile(r"BALANCED|HYBRID|AGGRESS.*HYBRID|EQUITY.*HYBRID", re.I), "wife_retirement"),
    # Mid Cap → child_education
    (re.compile(r"\bMID\s*CAP\b|MIDCAP|MID30|MIDOP|IGMCF", re.I),        "child_education"),
    # Small Cap → child_marriage (long horizon, high growth)
    (re.compile(r"\bSMALL\s*CAP\b|SMALLCAP", re.I),                       "child_marriage"),
    # Multi Cap → child_marriage
    (re.compile(r"MULTI\s*CAP|MULTICAP|\bACTF\b|ACTIV.*FUND", re.I),      "child_marriage"),
    # Sectoral / Thematic → None
    (re.compile(r"TECH(NOLOG)?|PHARMA|BANK(ING)?|FMCG|INFRA"
                r"|SECTOR|THEMATIC", re.I),                                None),
    # Debt / Liquid → None
    (re.compile(r"\bDEBT\b|LIQUID|MONEY\s*MARKET|OVERNIGHT|ULTRA\s*SHORT"
                r"|SHORT\s*DURAT|CREDIT\s*RISK|GILT|BOND\s*FUND", re.I),  None),
]


def suggest_goal_tag(isin: str | None, instrument_name: str | None) -> str | None:
    """Return the suggested goal_tag for a holding, or None if no rule matches."""
    if isin and isin in ISIN_GOAL_MAP:
        return ISIN_GOAL_MAP[isin]
    name = (instrument_name or "").strip()
    if not name:
        return None
    for pat, tag in _NAME_RULES:
        if pat.search(name):
            return tag
    return None


def auto_tag_holdings(db, owner_id: str | None = None) -> int:
    """
    Set goal_tag on all holdings where it is currently NULL.
    Holdings that already have a goal_tag (manually set by the user) are skipped.
    Returns the number of holdings updated.
    """
    from app.models.schemas import Account, Holding
    from sqlalchemy import func

    q = db.query(Holding).join(Account, Holding.account_id == Account.id)
    if owner_id:
        q = q.filter(Account.owner_id == owner_id)
    # Only touch untagged holdings
    q = q.filter(Holding.goal_tag.is_(None))

    updated = 0
    for h in q.all():
        tag = suggest_goal_tag(h.isin, h.instrument_name)
        if tag is not None:           # explicit None means "leave untagged"
            h.goal_tag = tag
            updated += 1

    if updated:
        db.commit()
        logger.info("auto_tag_holdings: tagged %d holdings (owner=%s)", updated, owner_id or "all")
    return updated
