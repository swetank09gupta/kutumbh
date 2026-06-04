"""
Insert break-even synthetic redemption credits for "ghost" Coin MF ISINs —
funds whose buy emails were captured but whose redemption emails are missing
from the inbox. Without these, our XIRR sees cost-basis with no terminal value,
producing a phantom -100% loss per ghost.

Assumptions:
  - Synthetic credit amount = sum of debit amounts for that ISIN (break-even).
  - Synthetic credit date    = last_buy_date + 90 days
                               (or today, whichever is earlier).
  - Synthetic txn is tagged with description "[SYNTH] break-even redemption"
    and reference_number "SYNTH:<isin>" so it can be identified / removed.

Identification of ghosts:
  - ISIN appears as DEBIT on a Coin account, AND
  - Same ISIN is absent from the latest deduped holdings snapshot.

Usage:
    docker exec finance_app python -m scripts.synth_ghost_redemptions
    docker exec finance_app python -m scripts.synth_ghost_redemptions --dry-run
"""
from __future__ import annotations

import argparse
import logging
from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import func

from app.engine.holdings_view import latest_holdings
from app.models.database import SessionLocal
from app.models.schemas import Account, Transaction

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger("synth_ghost_redemptions")

SYNTH_REF_PREFIX = "SYNTH:"
SYNTH_DESC       = "[SYNTH] break-even redemption (missing email)"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="Show what would be inserted, don't write.")
    args = ap.parse_args()

    db = SessionLocal()
    try:
        coin_accts = db.query(Account).filter(Account.institution.ilike("%Coin%")).all()
        if not coin_accts:
            log.warning("No Coin accounts found"); return

        for coin in coin_accts:
            # Wipe any prior synth rows for this account (idempotent re-runs).
            n_old = db.query(Transaction).filter(
                Transaction.account_id == coin.id,
                Transaction.reference_number.like(f"{SYNTH_REF_PREFIX}%"),
            ).delete(synchronize_session=False)
            if n_old:
                log.info("[%s] Cleared %d prior synth rows", coin.owner_id, n_old)

            # Per-ISIN debit aggregates on this Coin account.
            rows = db.query(
                Transaction.isin,
                func.sum(Transaction.amount),
                func.max(Transaction.date),
                func.count(Transaction.id),
            ).filter(
                Transaction.account_id == coin.id,
                Transaction.transaction_type == "debit",
                Transaction.isin.isnot(None),
            ).group_by(Transaction.isin).all()

            # Current MF holding ISINs for this owner.
            held = latest_holdings(db, owner_ids=[coin.owner_id])
            held_isins = {h.isin for h in held if h.holding_type == "mutual_fund" and h.isin}

            today = date.today()
            for isin, total, last_buy, n in rows:
                if isin in held_isins:
                    continue   # not a ghost
                amount = Decimal(str(total or 0))
                if amount <= 0:
                    continue
                synth_date = min(last_buy + timedelta(days=90), today)
                log.info("[%s] Ghost %s  buys=₹%-9.0f  last_buy=%s  →  synth credit ₹%.0f on %s",
                         coin.owner_id, isin, float(amount), last_buy, float(amount), synth_date)
                if not args.dry_run:
                    db.add(Transaction(
                        account_id        = coin.id,
                        date              = synth_date,
                        amount            = amount,
                        transaction_type  = "credit",
                        description       = SYNTH_DESC,
                        raw_description   = SYNTH_DESC,
                        counterparty      = None,
                        reference_number  = f"{SYNTH_REF_PREFIX}{isin}",
                        isin              = isin,
                    ))
        if not args.dry_run:
            db.commit()
            log.info("Committed synthetic redemption credits")
        else:
            log.info("DRY RUN — no DB changes made")
    finally:
        db.close()


if __name__ == "__main__":
    main()
