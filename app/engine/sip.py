"""
What each person actually invests every month, from the exact trade ledger.

A SIP is a fund bought on a regular cadence for a steady amount. Whole-portfolio debits are not
SIPs: one-off stock buys, lump sums and the bank→broker transfer that funds them used to be added
in, which inflated "current SIP" (₹51k/month for someone who invests ₹4k) and was then split
across everyone's goals. Here an (owner, fund) counts only when it was bought in at least half of
the look-back months with most months close to the usual amount.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta
from typing import Optional

from sqlalchemy.orm import Session

from app.ledger.rules import SOURCE_RANK, effective_trades, Trade
from app.models.schemas import InstrumentTrade

CLOSE = 0.25          # a month within 25% of the median month counts as 'the usual amount'


def _month_index(d: date) -> int:
    return d.year * 12 + d.month


def _buys(db: Session, owner_ids: list[str]) -> list[InstrumentTrade]:
    rows = (db.query(InstrumentTrade)
            .filter(InstrumentTrade.owner_id.in_(owner_ids), InstrumentTrade.side == "B",
                    InstrumentTrade.isin.like("INF%"), InstrumentTrade.amount.isnot(None))
            .all())
    # one source per event, exactly as the ledger counts them
    eff, _, _ = effective_trades([Trade(str(r.id), r.isin, r.trade_date, r.side, r.quantity, r.price,
                                        r.amount, r.source) for r in rows])
    keep = {t.id for t in eff}
    return [r for r in rows if str(r.id) in keep]


def monthly_sip_by_fund(db: Session, owner_ids: list[str], months: int = 6,
                        today: Optional[date] = None) -> dict[tuple[str, str], float]:
    """{(owner_id, isin): monthly SIP amount} for funds bought regularly in the last `months`."""
    today = today or date.today()
    first = _month_index(today) - months + 1
    per: dict[tuple[str, str], dict[int, float]] = defaultdict(lambda: defaultdict(float))
    for r in _buys(db, owner_ids):
        m = _month_index(r.trade_date)
        if first <= m <= _month_index(today):
            per[(r.owner_id, r.isin)][m] += float(r.amount)
    out: dict[tuple[str, str], float] = {}
    need = max(3, months // 2)
    last, prev = _month_index(today), _month_index(today) - 1
    for key, by_month in per.items():
        vals = sorted(by_month.values())
        # Current cadence: bought in at least half the window, or in each of the last two months.
        # A missed or doubled month (SIP skipped, one lump top-up) must not hide a live SIP, so the
        # amount is the median month, and most months have to sit close to it.
        if len(vals) < need and not (last in by_month and prev in by_month):
            continue
        median = vals[len(vals) // 2] if len(vals) % 2 else (vals[len(vals) // 2 - 1] + vals[len(vals) // 2]) / 2
        if median <= 0:
            continue
        close = sum(1 for v in vals if abs(v - median) <= CLOSE * median)
        if close < max(2, 0.6 * len(vals)):
            continue
        out[key] = round(median / 100.0) * 100          # SIPs are set in round amounts
    return out


def sip_stepup_pct(db: Session, owner_ids: list[str], today: Optional[date] = None) -> Optional[float]:
    """
    Annual growth of the regular-SIP funds' contributions: last 12 months vs the 12 before.
    None when either window has no data. Clamped to 0–12% (a new SIP is not a growth trend).
    """
    today = today or date.today()
    live = {k for k in monthly_sip_by_fund(db, owner_ids, 6, today)}
    if not live:
        return None
    cut_recent = today - timedelta(days=365)
    cut_prior = today - timedelta(days=730)
    recent = prior = 0.0
    for r in _buys(db, owner_ids):
        if (r.owner_id, r.isin) not in live:
            continue
        if r.trade_date >= cut_recent:
            recent += float(r.amount)
        elif r.trade_date >= cut_prior:
            prior += float(r.amount)
    if prior <= 0 or recent <= 0:
        return None
    return round(max(0.0, min(12.0, (recent / prior - 1.0) * 100.0)), 1)
