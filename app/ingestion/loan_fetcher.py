"""
Pull HDFC part-prepayment letters (PDF attachments) from the mailboxes into loan_anchors,
then apply the newest anchor to the loan liability (app.engine.loan).
"""
from __future__ import annotations

import email
import io
import logging
from datetime import date, timedelta

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.config import settings
from app.ingestion.email_fetcher import _all_pdf_passwords, _imap_connect, _parse_imap_fetch
from app.ingestion.parsers.loan_letter import parse_letter
from app.models.schemas import LoanAnchor

logger = logging.getLogger(__name__)


def _pdf_text(data: bytes, owner_id: str) -> str:
    import pdfplumber
    for pw in [""] + _all_pdf_passwords("HDFC", owner_id):
        try:
            with pdfplumber.open(io.BytesIO(data), password=pw or None) as pdf:
                return " ".join((pg.extract_text() or "") for pg in pdf.pages)
        except Exception:
            continue
    return ""


def ingest_loan_letters(db: Session, since_days: int = 30) -> dict[str, int]:
    since = (date.today() - timedelta(days=since_days)).strftime("%d-%b-%Y")
    rows = []
    for acc in settings.get_email_accounts():
        if "gmail" not in acc["imap_host"]:
            continue                                   # HDFC bank mail goes to the Gmail mailbox
        try:
            mail = _imap_connect(acc)
        except Exception as exc:
            logger.error("loan letters: cannot open %s: %s", acc["address"], exc)
            continue
        try:
            mail.select('"[Gmail]/All Mail"', readonly=True)
            _, data = mail.search(None, "SINCE", since, "FROM", '"hdfcbank"', "SUBJECT", '"Prepayment Letter"')
            for uid in (data[0].split() if data and data[0] else []):
                raw = _parse_imap_fetch(mail.fetch(uid, "(RFC822)")[1])
                if not raw:
                    continue
                for part in email.message_from_bytes(raw).walk():
                    fn = part.get_filename() or ""
                    if not fn.lower().endswith(".pdf"):
                        continue
                    letter = parse_letter(_pdf_text(part.get_payload(decode=True), acc["owner_id"]))
                    if letter:
                        rows.append({
                            "owner_id": acc["owner_id"], "lender": "HDFC", "loan_no": letter.loan_no,
                            "as_on": letter.as_on, "prepaid": letter.prepaid,
                            "principal_after": letter.principal_after, "emi": letter.emi, "rate": letter.rate,
                            "emis_left": letter.emis_left, "source_file": fn,
                            "natural_key": f"HDFC|{letter.loan_no}|{letter.as_on}|{letter.prepaid}"})
        finally:
            try:
                mail.logout()
            except Exception:
                pass
    import uuid
    new = 0
    for r in {r["natural_key"]: r for r in rows}.values():
        r["id"] = uuid.uuid4()
        new += db.execute(pg_insert(LoanAnchor.__table__).values(r)
                          .on_conflict_do_nothing(index_elements=["natural_key"])).rowcount
    db.commit()
    return {"letters": len(rows), "new": new}
