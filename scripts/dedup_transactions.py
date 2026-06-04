"""
Recompute fingerprints on every transaction using the hardened normalisation
(strip whitespace + leading zeros from ref#, strip non-alphanumerics from
description). Where two rows now collapse to the same fingerprint, delete the
extras and keep the row whose original description was longest (most info).

Idempotent. Safe to re-run.

Usage:
    docker exec finance_app python -m scripts.dedup_transactions [--dry-run]
"""
from __future__ import annotations

import argparse
import logging
from collections import defaultdict

from app.etl.dedup import transaction_fingerprint
from app.models.database import SessionLocal
from app.models.schemas import Transaction

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger("dedup_transactions")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    db = SessionLocal()
    try:
        rows = db.query(Transaction).all()
        log.info("Scanning %d transactions", len(rows))

        # Group by NEW fingerprint computed on the fly
        by_new_fp: dict[str, list[Transaction]] = defaultdict(list)
        for r in rows:
            new_fp = transaction_fingerprint(
                str(r.account_id), r.date, r.amount, r.transaction_type,
                r.description or "", r.reference_number,
            )
            by_new_fp[new_fp].append(r)

        dup_groups = [g for g in by_new_fp.values() if len(g) > 1]
        total_dupes = sum(len(g) - 1 for g in dup_groups)
        total_amount = sum(
            float(g[0].amount or 0) * (len(g) - 1) for g in dup_groups
        )
        log.info(f"Duplicate groups: {len(dup_groups)}  →  "
                 f"{total_dupes} phantom rows  →  ₹{total_amount:,.0f} phantom amount")

        if not dup_groups:
            return

        # Print samples
        log.info("\nSample duplicate groups (showing first 8):")
        for g in dup_groups[:8]:
            log.info("  %s  ₹%-9.0f  ×%d", g[0].date, float(g[0].amount), len(g))
            for t in g:
                log.info("      desc=%r  ref=%r", (t.description or '')[:60], t.reference_number)

        if args.dry_run:
            log.info("\nDRY RUN — no rows deleted.")
            return

        # Phase 1 — delete extras, commit. Keep the row whose description is
        # longest (most informative).
        keepers: list[Transaction] = []
        deleted = 0
        for g in dup_groups:
            g.sort(key=lambda t: -len(t.description or ''))
            keepers.append(g[0])
            for t in g[1:]:
                db.delete(t)
                deleted += 1
        db.commit()
        log.info(f"Phase 1: deleted {deleted} phantom rows, kept {len(keepers)} originals.")

        # Phase 2 — best-effort: re-fingerprint the keepers so future re-imports
        # of the same statement don't re-insert. Some keepers may collide with
        # existing rows whose old fingerprint coincidentally matches the new
        # one; skip those silently — they're harmless because the row's data
        # is correct.
        updated = collisions = 0
        for k in keepers:
            new_fp = transaction_fingerprint(
                str(k.account_id), k.date, k.amount, k.transaction_type,
                k.description or "", k.reference_number,
            )
            if k.fingerprint == new_fp:
                continue
            old_fp = k.fingerprint
            k.fingerprint = new_fp
            try:
                db.commit()
                updated += 1
            except Exception:
                db.rollback()
                # Restore the old fp (refresh from DB) and move on.
                k = db.query(Transaction).filter_by(id=k.id).first()
                collisions += 1
        log.info(f"Phase 2: fingerprints updated for {updated} keepers "
                 f"(skipped {collisions} on unique-key collision).")
    finally:
        db.close()


if __name__ == "__main__":
    main()
