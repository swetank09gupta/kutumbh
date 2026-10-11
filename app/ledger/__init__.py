"""Instrument ledger — see docs/adr/0023-instrument-ledger.md."""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def refresh(db) -> dict:
    """
    Reconcile, then roll holdings forward. Run after every ingest and daily
    (prices move), before net-worth snapshots. Never raises: a ledger problem must
    not stop statements from being stored.
    """
    out: dict = {}
    try:
        from app.ledger.engine import reconcile
        out["reconcile"] = reconcile(db)
    except Exception as exc:
        logger.error("Ledger reconcile failed: %s", exc, exc_info=True)
        db.rollback()
        out["reconcile"] = {"error": str(exc)}
    try:
        from app.ledger.rollforward import rollforward
        out["rollforward"] = rollforward(db)
    except Exception as exc:
        logger.error("Ledger roll-forward failed: %s", exc, exc_info=True)
        db.rollback()
        out["rollforward"] = {"error": str(exc)}
    return out
