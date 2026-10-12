"""
One-off: read the FD / PPF rows of EVERY SBI statement on disk into deposit_snapshots (new statements record
themselves). Oldest first, so a later statement never depends on an earlier one.

    docker exec finance_app python -m scripts.backfill_deposit_history
"""
from __future__ import annotations

import logging
from pathlib import Path

from sqlalchemy import text

from app.etl.deposit_history import record_snapshots
from app.ingestion.email_fetcher import _derive_pdf_password
from app.ingestion.parsers.sbi import SbiParser
from app.models.database import SessionLocal

logging.getLogger("app.ingestion.parsers").setLevel(logging.WARNING)


def main() -> dict:
    db = SessionLocal()
    done = failed = rows = 0
    try:
        files = db.execute(text("""SELECT DISTINCT a.owner_id, t.source_file FROM transactions t
            JOIN accounts a ON a.id = t.account_id WHERE a.institution = 'SBI' AND a.account_type = 'bank'""")).all()
        for owner, f in files:
            try:
                res = SbiParser(pdf_password=_derive_pdf_password(owner, "SBI")).parse(Path(f))
            except Exception:
                failed += 1
                continue
            rows += record_snapshots(db, owner, res.manual_assets)
            done += 1
        db.commit()
        return {"statements": done, "unreadable": failed, "snapshot_rows": rows,
                "in_table": db.execute(text("SELECT count(*) FROM deposit_snapshots")).scalar()}
    finally:
        db.close()


if __name__ == "__main__":
    print(main())
