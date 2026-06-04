"""
Merge duplicate accounts where one row has account_ref="unknown" and a
sibling row (same owner+institution+account_type) has a real reference.

Parsers fall back to "unknown" when they can't extract the account number
from a particular statement. Over time this creates a phantom account per
institution per owner that holds a slice of transactions while the real
account holds the rest. This script reattributes the phantom's txns,
holdings, and balances onto the canonical account, then deletes the phantom.

Idempotent. Safe to re-run.

Usage:
    docker exec finance_app python -m scripts.dedup_unknown_accounts [--dry-run]
"""
from __future__ import annotations

import argparse
import logging
from collections import defaultdict

from sqlalchemy import text

from app.models.database import SessionLocal
from app.models.schemas import Account, Balance, Holding, Transaction

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger("dedup_unknown_accounts")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    db = SessionLocal()
    try:
        accounts = db.query(Account).all()
        # Group by (owner, institution, account_type)
        groups: dict[tuple, list[Account]] = defaultdict(list)
        for a in accounts:
            groups[(a.owner_id, a.institution, a.account_type)].append(a)

        merges = 0
        for key, accs in groups.items():
            if len(accs) < 2:
                continue
            owner, inst, atype = key
            unknown = [a for a in accs if (a.account_ref or "").lower() == "unknown"]
            real    = [a for a in accs if (a.account_ref or "").lower() != "unknown"]
            if not unknown or not real:
                continue
            # Pick the canonical real account: prefer the one with most txns.
            canonical = max(real, key=lambda a:
                db.query(Transaction).filter(Transaction.account_id == a.id).count()
            )
            for u in unknown:
                nt = db.query(Transaction).filter(Transaction.account_id == u.id).count()
                nh = db.query(Holding).filter(Holding.account_id == u.id).count()
                nb = db.query(Balance).filter(Balance.account_id == u.id).count()
                log.info("MERGE  %s/%s/%s  unknown(%s) → %s  (%d txns, %d holdings, %d balances)",
                         owner, inst, atype, u.id, canonical.account_ref, nt, nh, nb)
                if args.dry_run:
                    continue
                # Reattribute via raw SQL — ON CONFLICT-safe upsert keys handle
                # the rare case where a txn fingerprint collides on the target.
                # Transaction.fingerprint is GLOBALLY unique, so reattributing
                # account_id never collides — safe straight UPDATE.
                db.execute(text("UPDATE transactions SET account_id = :new WHERE account_id = :old"),
                           {"new": str(canonical.id), "old": str(u.id)})
                # Holdings/balances have composite unique keys including account_id;
                # delete phantom rows that would collide on (snapshot_date, isin/_),
                # then move the remainder.
                db.execute(text("""
                    DELETE FROM holdings h
                    WHERE h.account_id = :old
                      AND EXISTS (
                        SELECT 1 FROM holdings c
                         WHERE c.account_id = :new
                           AND c.snapshot_date = h.snapshot_date
                           AND COALESCE(c.isin,'') = COALESCE(h.isin,'')
                      )
                """), {"new": str(canonical.id), "old": str(u.id)})
                db.execute(text("UPDATE holdings SET account_id = :new WHERE account_id = :old"),
                           {"new": str(canonical.id), "old": str(u.id)})
                db.execute(text("""
                    DELETE FROM balances b
                    WHERE b.account_id = :old
                      AND EXISTS (
                        SELECT 1 FROM balances c
                         WHERE c.account_id = :new
                           AND c.snapshot_date = b.snapshot_date
                      )
                """), {"new": str(canonical.id), "old": str(u.id)})
                db.execute(text("UPDATE balances SET account_id = :new WHERE account_id = :old"),
                           {"new": str(canonical.id), "old": str(u.id)})
                db.execute(text("DELETE FROM accounts WHERE id = :old"),
                           {"old": str(u.id)})
                merges += 1
        if not args.dry_run:
            db.commit()
        log.info("%s %d phantom 'unknown'-ref account(s)",
                 "Would merge" if args.dry_run else "Merged", merges)
    finally:
        db.close()


if __name__ == "__main__":
    main()
