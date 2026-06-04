"""
Refresh MF holding NAVs from AMFI's daily NAV master.

Zerodha holdings PDFs are only generated when the user downloads / requests
them, so `current_nav` and `current_value` in the holdings table can be weeks
or months stale. This script updates them to today's NAV (from AMFI), keeping
units unchanged, so XIRR/CAGR reflect today's market.

Usage:
    docker exec finance_app python -m scripts.refresh_mf_navs
"""
from __future__ import annotations

import logging
from datetime import date
from decimal import Decimal

from sqlalchemy import func

from app.engine.amfi_isin import get_nav, refresh as refresh_amfi
from app.models.database import SessionLocal
from app.models.schemas import Holding

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger("refresh_mf_navs")


def main() -> None:
    # Force a fresh AMFI pull so we're not using yesterday's cache.
    try:
        n = refresh_amfi()
        log.info("Refreshed AMFI: %d schemes", n)
    except Exception as exc:
        log.warning("AMFI refresh failed (using cached): %s", exc)

    db = SessionLocal()
    try:
        # Operate only on the latest snapshot per (account, ISIN) — older
        # historical snapshots represent point-in-time NAVs and must be
        # preserved for trend analysis.
        latest = db.query(func.max(Holding.snapshot_date)).scalar()
        if not latest:
            log.warning("No holdings present")
            return
        rows = (
            db.query(Holding)
            .filter(Holding.snapshot_date == latest,
                    Holding.holding_type == "mutual_fund",
                    Holding.isin.isnot(None))
            .all()
        )
        log.info("Found %d MF holdings on latest snapshot %s", len(rows), latest)

        updated = unmatched = skipped = 0
        total_value_delta = Decimal(0)
        unmatched_names: list[str] = []
        for h in rows:
            nav = get_nav(h.isin)
            if not nav or nav <= 0:
                unmatched += 1
                unmatched_names.append(f"{h.isin} {(h.instrument_name or h.symbol or '?')[:35]}")
                continue
            old_nav = float(h.current_nav or 0)
            if abs(old_nav - nav) < 0.01:
                skipped += 1
                continue
            units = Decimal(str(h.units or 0))
            new_nav = Decimal(f"{nav:.4f}")
            new_value = (units * new_nav).quantize(Decimal("0.01"))
            old_value = Decimal(h.current_value or 0)
            h.current_nav   = new_nav
            h.current_value = new_value
            total_value_delta += (new_value - old_value)
            updated += 1
            log.debug("  %s  NAV %.2f → %.2f  value ₹%.0f → ₹%.0f",
                      h.isin, old_nav, nav, float(old_value), float(new_value))
        db.commit()
        log.info("Updated %d / %d MF holdings (unchanged: %d, no AMFI match: %d)",
                 updated, len(rows), skipped, unmatched)
        log.info(f"Net value delta from NAV refresh: ₹{float(total_value_delta):+,.0f}")
        if unmatched_names:
            log.warning("Unmatched ISINs (no AMFI NAV):")
            for nm in unmatched_names[:20]:
                log.warning("  %s", nm)
    finally:
        db.close()


if __name__ == "__main__":
    main()
