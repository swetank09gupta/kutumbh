"""
/api/sync — trigger email fetch + ETL + net-worth snapshot.

POST /api/sync/                   → fires in background, returns immediately
POST /api/sync/historical         → fires in background (2-year import)
GET  /api/sync/status             → check if a sync is running + last result
"""
from __future__ import annotations

import logging
import os
import queue
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends
from sqlalchemy.orm import Session

from app.api.deps import get_db

RAW_DIR = Path("data/raw")

router = APIRouter(prefix="/api/sync", tags=["sync"])
logger = logging.getLogger(__name__)

# ── Chunked pipeline config ───────────────────────────────────────────────────
# Syncs longer than _CHUNK_THRESHOLD days are automatically split into
# _CHUNK_DAYS-day windows and processed as a pipelined fetch→persist stream.
# The fetcher runs one chunk ahead in a background thread so IMAP wait time
# overlaps with the previous chunk's parse+persist work.
# Both values are tunable via env vars without a rebuild.
_CHUNK_THRESHOLD = int(os.environ.get("SYNC_CHUNK_THRESHOLD", "90"))   # days
_CHUNK_DAYS      = int(os.environ.get("SYNC_CHUNK_DAYS",      "90"))   # days per window


def _time_chunks(since_days: int, until_days: int, chunk_days: int) -> list[tuple[int, int]]:
    """
    Split [until_days … since_days] into equal windows of chunk_days, oldest first.
    E.g. since=200, until=0, chunk=90 → [(200,110), (110,20), (20,0)]
    """
    chunks: list[tuple[int, int]] = []
    cursor = since_days
    while cursor > until_days:
        window_until = max(cursor - chunk_days, until_days)
        chunks.append((cursor, window_until))
        cursor = window_until
    return chunks


# ── In-process sync state tracker ────────────────────────────────────────────
_sync_lock   = threading.Lock()
_sync_state: dict = {
    "running":    False,
    "started_at": None,
    "finished_at": None,
    "last_result": None,
    "last_error":  None,
}


def _run_full_sync_bg(since_days: int, until_days: int = 0) -> None:
    """
    Background-safe sync — opens its own DB session so the
    HTTP request session lifecycle doesn't interfere.

    When since_days > SYNC_CHUNK_THRESHOLD (default 90), the range is
    split into SYNC_CHUNK_DAYS-day windows processed as a pipeline:
    a fetcher thread fills a 1-slot queue while the main thread parses
    and persists the previous chunk, so IMAP wait time is hidden behind
    CPU work.  Memory is bounded to 2 chunks at most.  Maintenance runs
    once at the end over the full dataset.
    """
    global _sync_state

    with _sync_lock:
        if _sync_state["running"]:
            logger.warning("Sync already running — skipping duplicate trigger")
            return
        _sync_state["running"]    = True
        _sync_state["started_at"] = datetime.now(timezone.utc).isoformat()
        _sync_state["last_error"] = None

    from app.models.database import SessionLocal
    from app.ingestion.email_fetcher import fetch_all_accounts
    from app.ingestion.attachment_router import process_attachments
    from app.engine.networth import snapshot_all
    from app.etl.maintenance import run_all as _run_maintenance

    db = SessionLocal()
    try:
        chunk_days = int(os.environ.get("SYNC_CHUNK_DAYS", str(_CHUNK_DAYS)))
        chunks     = _time_chunks(since_days, until_days, chunk_days) \
                     if since_days > _CHUNK_THRESHOLD else [(since_days, until_days)]
        n_chunks   = len(chunks)

        logger.info(
            "Sync started (since_days=%d, until_days=%d) — %d chunk(s) of ≤%d days",
            since_days, until_days, n_chunks, chunk_days,
        )

        totals = {"attachments_fetched": 0, "transactions_inserted": 0,
                  "holdings_inserted": 0, "parse_errors": 0}

        if n_chunks == 1:
            # ── Fast path: no thread overhead ────────────────────────────────
            attachments = fetch_all_accounts(since_days=since_days, until_days=until_days)
            logger.info("Fetched %d attachments", len(attachments))
            summary = process_attachments(attachments, db)
            totals["attachments_fetched"]    += len(attachments)
            totals["transactions_inserted"]  += summary.get("transactions", 0)
            totals["holdings_inserted"]      += summary.get("holdings", 0)
            totals["parse_errors"]           += summary.get("failed", 0)

        else:
            # ── Pipelined path: fetcher → queue → processor ───────────────────
            # Queue size 1: fetcher stays exactly one chunk ahead, bounding
            # peak memory to 2 chunks regardless of total range size.
            work_q: queue.Queue = queue.Queue(maxsize=1)

            def _fetcher() -> None:
                for i, (ch_since, ch_until) in enumerate(chunks, 1):
                    try:
                        logger.info(
                            "Chunk %d/%d: fetching window -%dd → -%dd",
                            i, n_chunks, ch_since, ch_until,
                        )
                        batch = fetch_all_accounts(since_days=ch_since, until_days=ch_until)
                        work_q.put((i, batch))       # blocks if processor is behind
                    except Exception as exc:
                        logger.error(
                            "Chunk %d/%d fetch failed: %s", i, n_chunks, exc, exc_info=True,
                        )
                        work_q.put((i, []))          # push empty batch so processor doesn't stall
                work_q.put(None)                     # sentinel — no more chunks

            fetch_thread = threading.Thread(target=_fetcher, name="sync-fetcher", daemon=True)
            fetch_thread.start()

            while True:
                item = work_q.get()
                if item is None:
                    break                            # all chunks consumed
                chunk_idx, attachments = item
                logger.info(
                    "Chunk %d/%d: processing %d attachments",
                    chunk_idx, n_chunks, len(attachments),
                )
                summary = process_attachments(attachments, db)
                totals["attachments_fetched"]   += len(attachments)
                totals["transactions_inserted"] += summary.get("transactions", 0)
                totals["holdings_inserted"]     += summary.get("holdings", 0)
                totals["parse_errors"]          += summary.get("failed", 0)
                logger.info(
                    "Chunk %d/%d done: +%d txns, +%d holdings, %d errors",
                    chunk_idx, n_chunks,
                    summary.get("transactions", 0),
                    summary.get("holdings", 0),
                    summary.get("failed", 0),
                )

            fetch_thread.join()

        # Bank alert e-mails: evidence for what the statements have not delivered yet.
        # A failure here must never block the statement pipeline.
        try:
            from app.ingestion.alert_fetcher import ingest_alerts
            totals["alerts"] = ingest_alerts(db, since_days=max(since_days, 7))
        except Exception as exc:
            logger.error("Bank alert ingestion failed: %s", exc, exc_info=True)
            db.rollback()

        # Maintenance and snapshot run once over the full dataset.
        # Ledger first: it rebuilds today's holdings, which the snapshot reads.
        from app.ledger import refresh as _ledger_refresh
        maintenance = _run_maintenance(db)       # clean/de-dupe/tag first…
        ledger      = _ledger_refresh(db)        # …then rebuild holdings…
        snapshots   = snapshot_all(db)           # …then snapshot net worth from them
        from app.engine.goals import refresh_goals
        goals_refreshed = refresh_goals(db)      # …then goals from the same holdings
        from app import consistency
        health = consistency.run(db)             # …and verify every page will agree

        result = {
            **totals,
            "ledger":            ledger,
            "goals":             goals_refreshed,
            "consistency":       {k: health[k] for k in ("ok", "failing", "warnings")},
            "snapshots_updated": len(snapshots),
            "since_days":        since_days,
            "until_days":        until_days,
            "chunks":            n_chunks,
            "maintenance":       maintenance,
        }
        logger.info("Sync done: %s", result)
        _sync_state["last_result"] = result

    except Exception as exc:
        logger.error("Sync failed: %s", exc, exc_info=True)
        _sync_state["last_error"] = str(exc)

    finally:
        db.close()
        _sync_state["running"]     = False
        _sync_state["finished_at"] = datetime.now(timezone.utc).isoformat()


# ── Routes ────────────────────────────────────────────────────────────────────

@router.post("/", summary="Start background sync (returns immediately)")
def trigger_sync(
    background_tasks: BackgroundTasks,
    since_days: int = 90,
    until_days: int = 0,
):
    """
    Fires a sync in the background — response comes back in <1s.
    Poll **GET /api/sync/status** to check progress and see the result.
    Default: last 90 days of emails.
    `until_days`: upper bound (0 = up to today). E.g. since_days=730,until_days=365 = 2yrs→1yr ago.
    """
    if _sync_state["running"]:
        return {"status": "already_running", "started_at": _sync_state["started_at"]}

    background_tasks.add_task(_run_full_sync_bg, since_days, until_days)
    return {"status": "started", "since_days": since_days, "until_days": until_days,
            "check_progress": "GET /api/sync/status"}


@router.post("/historical", summary="Import 2 years of history (background)")
def trigger_historical(
    background_tasks: BackgroundTasks,
    since_days: int = 730,
    until_days: int = 0,
):
    """
    Same as above but defaults to 730 days (2 years).
    Use this once on first setup.
    `until_days`: upper bound (0 = up to today).
    """
    if _sync_state["running"]:
        return {"status": "already_running", "started_at": _sync_state["started_at"]}

    background_tasks.add_task(_run_full_sync_bg, since_days, until_days)
    return {"status": "started", "since_days": since_days, "until_days": until_days,
            "check_progress": "GET /api/sync/status"}


@router.post("/reprocess", summary="Re-run ETL on already-downloaded files in data/raw/")
def reprocess_local(background_tasks: BackgroundTasks):
    """
    Skips IMAP — just runs the parser + ETL pipeline on every file
    already sitting in data/raw/<owner_id>/.  Useful after a container
    rebuild that wiped the in-memory sync state but left downloaded files intact.
    """
    if _sync_state["running"]:
        return {"status": "already_running", "started_at": _sync_state["started_at"]}
    background_tasks.add_task(_run_reprocess_bg)
    return {"status": "started", "check_progress": "GET /api/sync/status"}


def _detect_institution(path: Path, pdf_password: str = "") -> str | None:
    """
    Identify the issuing institution from filename heuristics, then PDF content.
    Returns None for files that are not financial statements (marketing etc.).

    Call order in reprocess:
      1st call — no password: use filename heuristics + regex to get a hint
      2nd call — with password: do a PDF content peek for accurate identification
    """
    import re
    name = path.name.lower()

    # ── Skip non-statement documents ──────────────────────────────────────────
    NON_STATEMENT_KEYWORDS = {
        # Marketing / security bulletins
        "infographic", "cyber_security", "best_practices",
        "newsletter", "bulletin", "brochure", "advisory",
        # Loan/EMI notifications — not transaction statements
        "emi_prepayment", "prepayment_letter",
        # Tax certificates (Form 16A / interest certificates) — not statements
        "it_prov", "it_final", "it_arhl",
        # OTP / verification PDFs from SBI
        "email_otp", "_otp_",
        # Quarterly/annual reports — not bank/MF statements
        "_q1_", "_q2_", "_q3_", "_q4_",
        # Zerodha operational PDFs — not demat/trading statements.
        # (contract notes are NOT here: they are the source of trade date/price
        # for the instrument ledger — see docs/adr/0023-instrument-ledger.md)
        "margin-statement", "ledger",
    }
    if any(kw in name for kw in NON_STATEMENT_KEYWORDS):
        logger.info("Skipping non-statement file: %s", path.name)
        return None

    # ── Permanently unreadable files (unknown password, skip silently) ────────
    SKIP_FILENAMES = {
        "Mon,_13_Mar_73117541403202301410713853130197747.pdf",
        "Fri,_17_Mar_73226331703202308370026940930197747.pdf",
        "14_Mar_2023_53132226320230434NB9146338474966CPIMBCP146356419.pdf",
        "14_Mar_2023_73908226320230434NB1646338474930CPIMBCP146356461.pdf",
        "19_Mar_2023_71315227320230934NB8854258474951CPIMBCP146544588.pdf",
    }
    if path.name in SKIP_FILENAMES:
        logger.debug("Skipping permanently unreadable file: %s", path.name)
        return None

    # ── Clear filename keywords (no I/O needed) ───────────────────────────────
    if "hdfc" in name:   return "HDFC"
    if "sbi" in name:    return "SBI"
    if "axis" in name:   return "Axis"
    if "union" in name:  return "Union Bank"
    # Coin allotment/redemption e-mails are HTML bodies named "<date>_coin_<id>.html";
    # they need ZerodhaCoinParser, not the Kite CSV/PDF parser.
    if "_coin_" in name and path.suffix.lower() in (".html", ".htm"): return "Zerodha Coin"
    if "_cdslalert_" in name and path.suffix.lower() in (".html", ".htm"): return "CDSL Alert"
    if "zerodha" in name or "kite" in name or "coin" in name: return "Zerodha"
    if "contract-note" in name: return "Zerodha"
    # Console tradebook exports (equity or MF). The ledger de-duplicates them against
    # contract notes / Coin mails, so importing them only ever fills gaps.
    if "tradebook" in name and path.suffix.lower() == ".csv": return "Zerodha"
    # Zerodha "statement of transactions" PDFs (2025+ naming): <date>_<client>-<dp+boid>-sot.pdf
    if name.endswith("-sot.pdf"): return "Zerodha"
    if "transaction-with-holding" in name or "holding-statement" in name: return "Zerodha"
    if "cams" in name or "kfintech" in name or "cas" in name: return "CAMS"
    # CDSL monthly CAS: MONYYYY_CASID_TXN.pdf  e.g. MAY2023_AA20662210_TXN.pdf
    if re.search(r'[a-z]{3}\d{4}_[a-z]{2}\d+_txn', name, re.I): return "CDSL"

    # ── SBI savings account e-statement filename patterns ─────────────────────
    # Files are prefixed with date: "Fri,_07_Jun_<original_name>.pdf"
    # Original name patterns:
    #   8553079866<DDMMYYYY>.pdf  → CIF-number + statement-date (14+ digits)
    #   PDF<long_digits>.pdf      → SBI Yono e-statement
    #   STMT_AC_<account>.pdf     → SBI loan account statement
    # CAMS sends transaction-confirmation PDFs named PDF<digits>.pdf
    if re.search(r'_PDF\d{8,}\.pdf$', path.name, re.IGNORECASE):
        return "CAMS"
    # SBI savings e-statement: CIF(10) + DDMMYYYY(8) = 18-19 digits (no "PDF" prefix)
    # HDFC credit card = 16 digits → require ≥17 to avoid false match
    if re.search(r'_\d{17,}\.pdf$', path.name, re.IGNORECASE):
        return "SBI"
    if "stmt_ac_" in name:
        return "SBI"

    # ── PDF content peek when password is available (most reliable) ───────────
    # Do this BEFORE the _XXXX.pdf regex so that Union Bank statements
    # (which also end in _XXXX.pdf) are correctly identified via UBIN/UNION BANK text.
    if pdf_password and path.suffix.lower() == ".pdf":
        try:
            import pdfplumber
            with pdfplumber.open(str(path), password=pdf_password) as pdf:
                text = (pdf.pages[0].extract_text() or "").upper() if pdf.pages else ""
            if "UNION BANK" in text or "UBIN" in text: return "Union Bank"
            if "STATE BANK"  in text or "SBI"  in text: return "SBI"
            if "AXIS BANK"   in text:                   return "Axis"
            if "HDFC BANK"   in text or "HDFC LTD" in text or "HDFCBANK" in text: return "HDFC"
            if "ZERODHA"     in text:                   return "Zerodha"
            if "CAMS"        in text or "KFINTECH" in text: return "CAMS"
        except Exception:
            pass

    # ── _XXXX.pdf — both HDFC and Union Bank use last-4-digits naming ─────────
    # Return "HDFC" as the password-derivation hint; HDFC and Union Bank share
    # the same formula (name4+DDMM), so the correct password will be derived.
    # The second call (with password) above will then identify it accurately.
    if re.search(r"_\d{4}\.pdf$", path.name, re.IGNORECASE):
        return "HDFC"

    # ── Unencrypted PDF fallback ───────────────────────────────────────────────
    if path.suffix.lower() == ".pdf":
        try:
            import pdfplumber
            with pdfplumber.open(str(path)) as pdf:
                text = (pdf.pages[0].extract_text() or "").upper() if pdf.pages else ""
            if "UNION BANK" in text or "UBIN" in text: return "Union Bank"
            if "STATE BANK"  in text: return "SBI"
            if "AXIS BANK"   in text: return "Axis"
            if "HDFC BANK"   in text: return "HDFC"
            if "ZERODHA"     in text: return "Zerodha"
            if "CAMS"        in text or "KFINTECH" in text: return "CAMS"
        except Exception:
            pass

    return None


def _run_reprocess_bg() -> None:
    """ETL-only pass over all files already in data/raw/ — no IMAP involved."""
    global _sync_state

    with _sync_lock:
        if _sync_state["running"]:
            return
        _sync_state["running"]    = True
        _sync_state["started_at"] = datetime.now(timezone.utc).isoformat()
        _sync_state["last_error"] = None

    from app.models.database import SessionLocal
    from app.ingestion.email_fetcher import FetchedAttachment
    from app.ingestion.attachment_router import process_attachments
    from app.engine.networth import snapshot_all
    from app.config import settings

    SUPPORTED = {".pdf", ".csv", ".xlsx", ".xls", ".html", ".htm"}

    db = SessionLocal()
    try:
        attachments: list[FetchedAttachment] = []

        for owner_dir in sorted(RAW_DIR.iterdir()):
            if not owner_dir.is_dir():
                continue
            owner_id = owner_dir.name
            # cache: pwd_key → (pwd, institution_hint) or ("__skip__", None)
            pdf_passwords_cache: dict[str, tuple[str, str | None]] = {}

            for fpath in sorted(owner_dir.iterdir()):
                if fpath.suffix.lower() not in SUPPORTED:
                    continue

                # Step 1: quick filename-only detection to derive the right password
                pwd_key = f"{owner_id}:{fpath.name}"
                if pwd_key not in pdf_passwords_cache:
                    institution_hint = _detect_institution(fpath)  # no password yet
                    if institution_hint is None:
                        logger.info("Reprocess: skipping non-statement file %s", fpath.name)
                        pdf_passwords_cache[pwd_key] = ("__skip__", None)
                    else:
                        from app.ingestion.email_fetcher import _derive_pdf_password
                        pwd = _derive_pdf_password(owner_id, institution_hint)
                        pdf_passwords_cache[pwd_key] = (pwd, institution_hint)

                cached_pwd, institution_hint = pdf_passwords_cache[pwd_key]
                if cached_pwd == "__skip__":
                    continue

                pwd = cached_pwd
                # Step 2: re-detect with password — content peek now works.
                # For _XXXX.pdf files (hint=HDFC), both HDFC and Union Bank use the
                # same naming pattern. Try all owners' HDFC+UB passwords so a statement
                # that arrived in one owner's inbox but belongs to another is identified.
                from app.ingestion.email_fetcher import _derive_pdf_password, _all_pdf_passwords
                if institution_hint == "HDFC":
                    passwords_for_peek = list(dict.fromkeys(
                        [pwd]
                        + _all_pdf_passwords("hdfc", owner_id)
                        + _all_pdf_passwords("union bank", owner_id)
                    ))
                else:
                    passwords_for_peek = [pwd]

                institution = None
                for peek_pwd in passwords_for_peek:
                    detected = _detect_institution(fpath, peek_pwd)
                    if detected and detected != "HDFC":
                        institution = detected
                        pwd = peek_pwd
                        break
                    elif detected == "HDFC":
                        institution = detected  # keep as fallback but keep looking

                if institution is None:
                    logger.info("Reprocess: could not identify institution for %s — skipping", fpath.name)
                    continue

                acct_type = ("mutual_fund" if institution in ("CAMS", "KFintech", "Zerodha Coin")
                             else "demat"   if institution in ("CDSL", "CDSL Alert")
                             else "stocks"  if institution == "Zerodha"
                             else "bank")

                logger.info("Reprocess: %s → %s (pwd=%s)", fpath.name, institution, "set" if pwd else "none")
                attachments.append(FetchedAttachment(
                    owner_id=owner_id,
                    institution=institution,
                    account_type=acct_type,
                    sender="(local)",
                    subject="(reprocess)",
                    email_date="",
                    file_path=fpath,
                    filename=fpath.name,
                    pdf_password=pwd,
                ))

        logger.info("Reprocess: found %d local files to process", len(attachments))
        summary   = process_attachments(attachments, db)
        from app.ledger import refresh as _ledger_refresh
        from app.etl.maintenance import run_all as _run_maintenance
        maintenance = _run_maintenance(db)       # same order as a full sync
        ledger    = _ledger_refresh(db)
        snapshots = snapshot_all(db)
        from app.engine.goals import refresh_goals
        goals_refreshed = refresh_goals(db)
        from app import consistency
        health = consistency.run(db)

        result = {
            "attachments_fetched":   len(attachments),
            "transactions_inserted": summary.get("transactions", 0),
            "holdings_inserted":     summary.get("holdings", 0),
            "parse_errors":          summary.get("failed", 0),
            "snapshots_updated":     len(snapshots),
            "since_days":            0,
            "ledger":                ledger,
            "goals":                 goals_refreshed,
            "consistency":           {k: health[k] for k in ("ok", "failing", "warnings")},
            "maintenance":           maintenance,
        }
        logger.info("Reprocess done: %s", result)
        _sync_state["last_result"] = result

    except Exception as exc:
        logger.error("Reprocess failed: %s", exc, exc_info=True)
        _sync_state["last_error"] = str(exc)
    finally:
        db.close()
        _sync_state["running"]     = False
        _sync_state["finished_at"] = datetime.now(timezone.utc).isoformat()


@router.get("/status", summary="Check sync progress and last result")
def sync_status():
    """
    Returns whether a sync is currently running and the result of the last one.
    Poll this after triggering a sync.
    """
    return {
        "running":     _sync_state["running"],
        "started_at":  _sync_state["started_at"],
        "finished_at": _sync_state["finished_at"],
        "last_result": _sync_state["last_result"],
        "last_error":  _sync_state["last_error"],
    }


@router.post("/backfill-snapshots", summary="Backfill historical net-worth snapshots from dated holdings")
def backfill_snapshots_route(db=Depends(get_db)):
    """
    Computes net-worth snapshots for every date where holdings data exists
    (e.g. monthly CDSL CAS files). Skips dates already computed.
    Run once after a full historical reprocess.
    """
    from app.engine.networth import backfill_snapshots
    created = backfill_snapshots(db)
    return {"snapshots_created": created}


@router.post("/recategorize", summary="Re-run categorizer on all existing transactions")
def recategorize_all(db=Depends(get_db)):
    """
    Re-applies the categorization rules to every transaction already in the DB.
    Use this after updating categorizer.py rules.
    """
    from app.etl.categorizer import categorise
    from app.models.schemas import Transaction

    rows = db.query(Transaction).all()
    updated = 0
    for txn in rows:
        cat = categorise(txn.description or "")
        if txn.category != cat.category or txn.subcategory != cat.subcategory:
            txn.category    = cat.category
            txn.subcategory = cat.subcategory
            updated += 1
    db.commit()
    logger.info("Recategorize: updated %d / %d transactions", updated, len(rows))
    return {"total": len(rows), "updated": updated}
