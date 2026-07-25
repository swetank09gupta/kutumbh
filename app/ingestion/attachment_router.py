"""
Routes each downloaded attachment to the correct parser,
then persists the results via the ETL pipeline.

Parsing is CPU/IO-bound and independent per file, so process_attachments
fans out parse work across a thread pool (default 6 workers, tunable via
PARSE_WORKERS env var).  Persistence stays serial on one DB session to
avoid account-creation races and keep transaction ordering predictable.
"""
from __future__ import annotations

import logging
import os
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from app.ingestion.email_fetcher import _worker_count

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

    # NOTE: a previous version of this file skipped "_<reqnum>_<last4>.pdf"
    # files on the assumption they were HDFC credit-card statements. That was
    # incorrect — those files are Union Bank statements (filename suffix is the
    # Union Bank account's last 4 digits, not an HDFC CC). The skip has been
    # removed so they parse normally via UnionBankParser.

    # Skip known non-statement document types that are either unreadable or
    # contain no transaction history worth parsing.
    fname = fpath.name
    if "hdfc" in inst and re.search(
        r"(FT\d|CR\d|IT_FINAL|IT_PROV|IT_ARHL)", fname, re.IGNORECASE
    ):
        # HDFC non-statement documents: fund-transfer receipts (FT3/4/5*),
        # credit-card charge receipts (CR*), and interest/tax certificates
        # (IT_FINAL, IT_PROV, IT_ARHL).  None contain transaction history.
        logger.debug("Skipping %s: HDFC non-statement document", fname)
        return None
    if re.search(r"TDS.CERTIFICATE", fname, re.IGNORECASE):
        # Bank TDS/interest certificates (Axis, HDFC, SBI): tax documents, not statements.
        logger.debug("Skipping %s: TDS certificate (no transaction data)", fname)
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

    # Union Bank sends small (< 50 KB) AES-256-encrypted notification/alert
    # letters (cheque clearance, debit alerts, KYC reminders) with a unique
    # per-document password embedded in the email body.  These are not
    # transaction statements — skip them silently rather than counting as errors.
    if "union" in inst and fpath.stat().st_size < 50_000:
        logger.info(
            "Skipping %s (%s): likely notification letter (%.1f KB, all passwords failed)",
            fpath.name, inst, fpath.stat().st_size / 1024,
        )
        return None

    logger.error(
        "Parser error for %s (%s): all %d passwords failed — %s",
        fpath.name, inst, len(passwords_to_try), last_exc,
    )
    return None


# PDF parsing is CPU/IO-mixed: default to 1 worker per logical CPU, hard cap 8.
# Override with PARSE_WORKERS env var (e.g. PARSE_WORKERS=2 on a 512 MB VPS).
_PARSE_WORKERS = _worker_count("PARSE_WORKERS", cpu_ratio=1.0, cap=8)


def _parse_one(item: tuple[int, FetchedAttachment]) -> tuple[int, FetchedAttachment, object]:
    """Worker: parse one attachment and return (original_index, attachment, result)."""
    idx, att = item
    result = route_and_parse(att)
    if result is not None:
        result.source_file = str(att.file_path)
    return idx, att, result


def process_attachments(attachments: list[FetchedAttachment], db) -> dict[str, int]:
    """
    Parse all attachments (parallel) then persist to DB (serial).

    Parsing is CPU/IO-bound and fully independent per file — a thread pool
    runs all parsers concurrently.  Persistence stays on a single DB session
    to avoid account-creation races and keep insertion order predictable.
    Results are persisted in the original attachment order so determinism is
    maintained across reruns.
    """
    from app.etl.normalizer import persist_parse_result

    summary = {"parsed": 0, "failed": 0, "transactions": 0, "holdings": 0}
    if not attachments:
        return summary

    n_workers = min(_PARSE_WORKERS, len(attachments))
    parse_results: list[tuple[FetchedAttachment, object] | None] = [None] * len(attachments)

    with ThreadPoolExecutor(max_workers=n_workers) as pool:
        futures = {pool.submit(_parse_one, (i, att)): i for i, att in enumerate(attachments)}
        for future in as_completed(futures):
            try:
                idx, att, result = future.result()
                parse_results[idx] = (att, result)
            except Exception as exc:
                orig_idx = futures[future]
                logger.error("Unexpected parse worker error (idx=%d): %s", orig_idx, exc, exc_info=True)
                parse_results[orig_idx] = (attachments[orig_idx], None)

    for att, result in parse_results:
        if result is None:
            summary["failed"] += 1
            continue
        counts = persist_parse_result(result, att.owner_id, db)
        summary["parsed"] += 1
        summary["transactions"] += counts.get("transactions", 0)
        summary["holdings"]     += counts.get("holdings", 0)

    return summary
