"""
One-off: fill `accrued_interest` (the statement's Interest Accrued column) on the existing SBI deposit rows by
re-reading the latest SBI statement of each owner. New statements fill it automatically.

    docker exec finance_app python -m scripts.backfill_fd_accrued
"""
from __future__ import annotations

from pathlib import Path

from sqlalchemy import text

from app.ingestion.attachment_router import process_attachments
from app.ingestion.email_fetcher import FetchedAttachment, _derive_pdf_password
from app.models.database import SessionLocal


def main() -> dict:
    db = SessionLocal()
    try:
        files = db.execute(text("""
            SELECT DISTINCT ON (a.owner_id) a.owner_id, t.source_file
            FROM transactions t JOIN accounts a ON a.id = t.account_id
            WHERE a.institution = 'SBI' AND a.account_type = 'bank'
            ORDER BY a.owner_id, t.date DESC""")).all()
        atts = [FetchedAttachment(owner_id=o, institution="SBI", account_type="bank", sender="", subject="",
                                  email_date="", file_path=Path(f), filename=Path(f).name,
                                  pdf_password=_derive_pdf_password(o, "SBI")) for o, f in files]
        out = process_attachments(atts, db)
        filled = db.execute(text("SELECT count(*) FROM manual_assets WHERE asset_type='fd' AND accrued_interest IS NOT NULL")).scalar()
        return {"statements": len(atts), "result": out, "deposits_with_accrued_interest": filled}
    finally:
        db.close()


if __name__ == "__main__":
    print(main())
