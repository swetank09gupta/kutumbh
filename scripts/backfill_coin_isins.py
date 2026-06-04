"""
Backfill ISIN on existing Zerodha Coin transactions using AMFI's NAV master.

Coin emails carry the fund name + folio but no ISIN, so until now per-instrument
XIRR could only match Coin txns to a Holding via fuzzy name normalization
(closing 9/25 holdings). Run this once after pulling new AMFI data to set
Transaction.isin directly, which lifts attribution to the full coverage AMFI
can resolve.

Usage:
    docker exec finance_app python -m scripts.backfill_coin_isins
"""
from __future__ import annotations

import logging

from app.engine.amfi_isin import lookup_isin
from app.models.database import SessionLocal
from app.models.schemas import Account, Transaction

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger("backfill_coin_isins")


def main() -> None:
    db = SessionLocal()
    try:
        coin_accounts = (
            db.query(Account)
            .filter(Account.institution.ilike("%Coin%"))
            .all()
        )
        if not coin_accounts:
            log.warning("No Coin accounts found")
            return
        acc_ids = [a.id for a in coin_accounts]
        log.info("Scanning %d Coin account(s)", len(acc_ids))

        rows = (
            db.query(Transaction)
            .filter(Transaction.account_id.in_(acc_ids),
                    Transaction.isin.is_(None),
                    Transaction.counterparty.isnot(None))
            .all()
        )
        log.info("Found %d Coin txns missing ISIN", len(rows))

        updated = 0
        unresolved: dict[str, int] = {}
        for r in rows:
            isin = lookup_isin(r.counterparty)
            if isin:
                r.isin = isin
                updated += 1
            else:
                unresolved[r.counterparty] = unresolved.get(r.counterparty, 0) + 1
        db.commit()
        log.info("Updated %d / %d Coin txns with ISIN", updated, len(rows))
        if unresolved:
            log.warning("Unresolved fund names (top 10):")
            for name, n in sorted(unresolved.items(), key=lambda kv: -kv[1])[:10]:
                log.warning("  %4d  %s", n, name)
    finally:
        db.close()


if __name__ == "__main__":
    main()
