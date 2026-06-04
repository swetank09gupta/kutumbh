"""
Re-run the categorizer on every transaction and update category/subcategory.

Useful after editing app/etl/categorizer.py rules. Idempotent.

Usage:
    docker exec finance_app python -m scripts.recategorize_transactions [--only-other]
"""
from __future__ import annotations

import argparse
import logging
from collections import Counter

from app.etl.categorizer import categorise
from app.models.database import SessionLocal
from app.models.schemas import Transaction

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger("recategorize")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only-other", action="store_true",
                    help="Only re-categorize transactions currently in 'other'/NULL.")
    args = ap.parse_args()

    db = SessionLocal()
    try:
        q = db.query(Transaction)
        if args.only_other:
            q = q.filter(Transaction.category.in_(["other", None, ""]))
        rows = q.all()
        log.info("Scanning %d transactions", len(rows))

        changed = 0
        rescue = Counter()  # bucket of "other → X" reclassifications
        for t in rows:
            new_cat = categorise(t.description or "")
            if t.category != new_cat.category or t.subcategory != new_cat.subcategory:
                if (t.category or "other") in ("other", None, "") \
                        and new_cat.category not in ("other", None, ""):
                    rescue[new_cat.category] += 1
                t.category    = new_cat.category
                t.subcategory = new_cat.subcategory
                changed += 1
        db.commit()
        log.info("Updated %d transactions", changed)
        if rescue:
            log.info("Reclassified out of 'other':")
            for cat, n in rescue.most_common():
                log.info("  %4d  → %s", n, cat)
    finally:
        db.close()


if __name__ == "__main__":
    main()
