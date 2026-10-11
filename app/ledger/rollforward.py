"""
Roll-forward: materialise today's holdings from the last statement plus the trades since.

Writes rows into the existing `holdings` table (source_file 'ledger:rollforward',
snapshot_date = today) so every downstream consumer — API, net worth, XIRR,
goals, the latest_holdings_deduped view — sees current positions with no change.
Statement rows are never touched; this engine only rewrites its own rows.
"""
from __future__ import annotations

import logging
from datetime import date
from decimal import Decimal
from typing import Optional

import httpx
from sqlalchemy.orm import Session

from app.ledger.engine import LEDGER_SOURCE_PREFIX, OwnerLedger, compute_owner
from app.ledger.prices import price_for
from app.ledger.rules import QTY_TOL, ZERO
from app.models.schemas import Account, Holding

logger = logging.getLogger(__name__)

ROLLFORWARD_SOURCE = f"{LEDGER_SOURCE_PREFIX}rollforward"


def _zerodha_account(db: Session, owner_id: str) -> Optional[Account]:
    return (
        db.query(Account)
        .filter(Account.owner_id == owner_id, Account.institution == "Zerodha",
                Account.account_type == "stocks")
        .order_by(Account.account_ref)
        .first()
    )


def _q(v: Optional[Decimal], places: str) -> Optional[Decimal]:
    return None if v is None else v.quantize(Decimal(places))


def build_rows(led: OwnerLedger, db: Session, today: date) -> list[dict]:
    """Pure-ish: one dict per open position (needs DB only for goal tags)."""
    rows: list[dict] = []
    tags = {
        isin: tag for isin, tag in (
            db.query(Holding.isin, Holding.goal_tag)
            .join(Account, Holding.account_id == Account.id)
            .filter(Account.owner_id == led.owner_id, Holding.goal_tag.isnot(None)).distinct().all()
        )
    }
    with httpx.Client(timeout=5.0, headers={"User-Agent": "Mozilla/5.0"}, follow_redirects=True) as client:
        for isin, st in sorted(led.states.items()):
            pos = st.position
            if pos.qty <= QTY_TOL:
                continue
            meta = led.meta.get(isin, {})
            px = price_for(isin, _ticker_symbol(led, isin), pos.last_price, pos.last_price_date,
                           meta.get("stmt_rate"), led.anchor_date, client)
            price, src, _as_of = px if px else (None, None, None)
            value = _q(pos.qty * price, "0.01") if price else None
            invested = _q(pos.invested, "0.01")
            gain = _q(value - invested, "0.01") if (value is not None and invested is not None) else None
            gain_pct = _q(gain / invested * 100, "0.0001") if (gain is not None and invested) else None
            rows.append({
                "isin": isin,
                "symbol": meta.get("symbol") or meta.get("name"),
                "instrument_name": meta.get("name") or isin,
                "holding_type": meta.get("holding_type") or ("mutual_fund" if isin.startswith("INF")
                                                            else "bonds" if isin.startswith("IN0") else "equity"),
                "units": _q(pos.qty, "0.0001"),
                "avg_cost_nav": _q(pos.avg_cost, "0.0001"),
                "current_nav": _q(price, "0.0001"),
                "current_value": value if value is not None else ZERO,
                "invested_value": invested,
                "gain_loss": gain, "gain_loss_pct": gain_pct,
                "first_buy_date": pos.first_buy_date,
                "cost_basis_quality": pos.quality,
                "price_source": src,
                "goal_tag": tags.get(isin),
            })
    return rows


def _ticker_symbol(led: OwnerLedger, isin: str) -> Optional[str]:
    """NSE ticker from the ledger's own trade symbols (contract notes carry it)."""
    for t in reversed(led.trades):
        if t.isin == isin and t.symbol:
            return t.symbol
    return None


def rollforward(db: Session, owner_ids: Optional[list[str]] = None, today: Optional[date] = None) -> dict:
    today = today or date.today()
    from app.models.schemas import Owner
    owners = owner_ids or [o for (o,) in db.query(Owner.id).filter(Owner.id != "family").all()]
    summary = {"as_of": str(today), "owners": {}}
    for oid in owners:
        acct = _zerodha_account(db, oid)
        if acct is None:
            continue
        led = compute_owner(db, oid)
        if led.anchor_date is None and not led.states:
            continue
        rows = build_rows(led, db, today)

        # Drop our previous output for this account, then rewrite. A statement row
        # that already exists for today wins (it is authoritative), so skip those ISINs.
        db.query(Holding).filter(Holding.account_id == acct.id,
                                 Holding.source_file == ROLLFORWARD_SOURCE).delete(synchronize_session=False)
        stmt_today = {
            i for (i,) in db.query(Holding.isin).filter(Holding.account_id == acct.id,
                                                        Holding.snapshot_date == today).all()
        }
        written = skipped = 0
        for r in rows:
            if r["isin"] in stmt_today:
                skipped += 1
                continue
            db.add(Holding(account_id=acct.id, snapshot_date=today, source_file=ROLLFORWARD_SOURCE, **r))
            written += 1
        db.flush()
        try:
            from app.engine.holding_tagger import auto_tag_holdings
            auto_tag_holdings(db, oid)
        except Exception as exc:                       # tagging must never block the roll-forward
            logger.warning("auto-tag after roll-forward failed: %s", exc)
        partial = sum(1 for r in rows if r["cost_basis_quality"] == "partial")
        summary["owners"][oid] = {"anchor_statement": str(led.anchor_date), "positions": len(rows),
                                  "written": written, "statement_rows_kept": skipped,
                                  "cost_exact": len(rows) - partial, "cost_partial": partial}
    db.commit()
    return summary
