"""
Routes each downloaded attachment to the correct parser,
then persists the results via the ETL pipeline.
"""
from __future__ import annotations

import logging
from pathlib import Path

from app.ingestion.email_fetcher import FetchedAttachment
from app.ingestion.parsers.base import ParseResult

logger = logging.getLogger(__name__)


def _make_parser(inst: str, pwd: str, fpath: Path | None = None):
    """Return an instantiated parser for the given institution and password."""
    if "hdfc" in inst:
        from app.ingestion.parsers.hdfc import HdfcParser
        return HdfcParser(pdf_password=pwd)
    if "sbi" in inst:
        from app.ingestion.parsers.sbi import SbiParser
        return SbiParser(pdf_password=pwd)
    if "axis" in inst:
        from app.ingestion.parsers.axis import AxisParser
        return AxisParser(pdf_password=pwd)
    if "union" in inst:
        from app.ingestion.parsers.union_bank import UnionBankParser
        return UnionBankParser(pdf_password=pwd)
    if "zerodha coin" in inst:
        from app.ingestion.parsers.zerodha_coin import ZerodhaCoinParser
        return ZerodhaCoinParser()
    if "zerodha" in inst:
        from app.ingestion.parsers.zerodha import ZerodhaParser
        return ZerodhaParser(pdf_password=pwd)
    if inst == "cdsl":
        from app.ingestion.parsers.cdsl_cas import CdslCasParser
        return CdslCasParser(pdf_password=pwd)
    if inst in ("cams", "kfintech"):
        # Per-folio KFintech statement vs full CAS — peek at page 1 text
        try:
            import pdfplumber
            with pdfplumber.open(str(fpath), password=pwd) as _pdf:
                _p1 = (_pdf.pages[0].extract_text() or "") if _pdf.pages else ""
        except Exception:
            _p1 = ""
        if "Statement of Account for Period" in _p1:
            from app.ingestion.parsers.kfintech_folio import KfintechFolioParser
            return KfintechFolioParser(pdf_password=pwd)
        from app.ingestion.parsers.cas_mf import CasMfParser
        return CasMfParser(pdf_password=pwd)
    return None


def route_and_parse(attachment: FetchedAttachment) -> ParseResult | None:
    """
    Determine which parser to use for an attachment and run it.
    If the primary password fails, automatically retries with every other
    owner's derived password — handles the case where a statement lands in
    one owner's email but is password-protected with another owner's details.
    Returns a ParseResult or None if parsing failed / unsupported.
    """
    from app.ingestion.email_fetcher import _all_pdf_passwords

    inst  = attachment.institution.lower()
    fpath = attachment.file_path

    # Quick check: is the institution supported at all?
    _SUPPORTED = {"hdfc", "sbi", "axis", "union", "zerodha coin", "zerodha", "cdsl", "cams", "kfintech"}
    if not any(s in inst for s in _SUPPORTED):
        logger.warning("Unknown institution '%s', no parser available", inst)
        return None

    # Try primary password first, then fall back to other owners' passwords.
    passwords_to_try = [attachment.pdf_password] + [
        p for p in _all_pdf_passwords(inst, attachment.owner_id)
        if p != attachment.pdf_password
    ]

    last_exc: Exception | None = None
    for pwd in passwords_to_try:
        try:
            parser = _make_parser(inst, pwd, fpath)
            # parse_cached() returns a pickled ParseResult if the file bytes
            # haven't changed since the last successful parse. First-time parse
            # falls through to parser.parse() normally.
            result = parser.parse_cached(fpath)
            if pwd != attachment.pdf_password:
                logger.info(
                    "Password fallback succeeded for %s: tried %d passwords",
                    fpath.name, passwords_to_try.index(pwd) + 1,
                )
            return result
        except Exception as exc:
            # Only retry on password errors; propagate other failures immediately.
            if "password" in str(exc).lower() or "PDFPasswordIncorrect" in type(exc).__name__:
                last_exc = exc
                continue
            logger.error("Parser error for %s (%s): %s", fpath.name, inst, exc, exc_info=True)
            return None

    logger.error(
        "Parser error for %s (%s): all %d passwords failed — %s",
        fpath.name, inst, len(passwords_to_try), last_exc,
    )
    return None


def process_attachments(attachments: list[FetchedAttachment], db) -> dict[str, int]:
    """
    Parse all attachments and persist to DB.
    Returns summary counts.
    """
    from app.etl.normalizer import persist_parse_result

    summary = {"parsed": 0, "failed": 0, "transactions": 0, "holdings": 0}

    for att in attachments:
        result = route_and_parse(att)
        if result is None:
            summary["failed"] += 1
            continue

        result.source_file = str(att.file_path)
        counts = persist_parse_result(result, att.owner_id, db)
        summary["parsed"] += 1
        summary["transactions"] += counts.get("transactions", 0)
        summary["holdings"]     += counts.get("holdings", 0)

    return summary
