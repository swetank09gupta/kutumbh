"""Shared data structures returned by every parser."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Optional


@dataclass
class ParsedTransaction:
    date:             date
    amount:           Decimal
    transaction_type: str            # "debit" | "credit"
    description:      str
    raw_description:  str
    reference_number: Optional[str]  = None
    value_date:       Optional[date] = None
    balance_after:    Optional[Decimal] = None
    counterparty:     Optional[str]  = None
    isin:             Optional[str]  = None  # set for equity demat transactions
    # Instrument-ledger fields (set for equity/MF trades and demat movements)
    quantity:         Optional[Decimal] = None  # shares / units traded or moved
    price:            Optional[Decimal] = None  # gross per-unit price / NAV
    running_qty:      Optional[Decimal] = None  # demat balance after this movement
    kind:             Optional[str]  = None  # contract_note | coin | tradebook | demat
    seq:              Optional[int]  = None  # line order within one ISIN block of a demat statement
    settle_date:      Optional[date] = None  # contract notes print the settlement date


@dataclass
class ParsedHolding:
    instrument_name: str
    units:           Decimal
    current_value:   Decimal
    holding_type:    str             # "equity" | "mutual_fund"
    symbol:          Optional[str]  = None
    isin:            Optional[str]  = None
    avg_cost_nav:    Optional[Decimal] = None
    current_nav:     Optional[Decimal] = None
    invested_value:  Optional[Decimal] = None
    gain_loss:       Optional[Decimal] = None
    gain_loss_pct:   Optional[Decimal] = None


@dataclass
class ParsedBalance:
    balance:       Decimal
    snapshot_date: date


@dataclass
class ParsedManualAsset:
    """
    An asset extracted directly from a bank statement
    (e.g. SBI FD/PPF section) to be upserted into manual_assets.
    account_ref is used as the dedup key (last 4 digits of FD account number).
    """
    asset_type:      str            # "fd" | "ppf"
    institution:     str
    name:            str            # human-readable, e.g. "SBI FD ...6925"
    current_value:   Decimal
    account_ref:     Optional[str]  = None   # last 4 digits for dedup / update
    interest_rate:   Optional[Decimal] = None
    start_date:      Optional[date] = None
    maturity_date:   Optional[date] = None
    maturity_amount: Optional[Decimal] = None
    accrued_interest: Optional[Decimal] = None   # "Interest Accrued" column: earned, not yet in the principal
    statement_date:  Optional[date] = None   # to prevent older statements overwriting newer


@dataclass
class ParseResult:
    institution:  str
    account_type: str             # "bank" | "stocks" | "mutual_fund" | "ppf" | "fd"
    account_ref:  Optional[str]  = None
    transactions: list[ParsedTransaction]  = field(default_factory=list)
    holdings:     list[ParsedHolding]      = field(default_factory=list)
    manual_assets: list[ParsedManualAsset] = field(default_factory=list)
    closed_account_refs: list[str]         = field(default_factory=list)  # FD acct last4 to remove
    balance:      Optional[ParsedBalance] = None
    source_file:  str = ""
    parse_warnings: list[str]              = field(default_factory=list)
    complete_holdings_snapshot: bool       = False  # True = replace all existing holdings for this account
    statement_date: Optional[date]         = None   # actual "as-on" date from the statement


class BaseParser:
    """Abstract base — subclasses implement `parse(path) -> ParseResult`."""

    institution:  str = "Unknown"
    account_type: str = "bank"

    # Bump when the ParseResult / Parsed* dataclasses change shape — invalidates
    # all cached pickles so we don't deserialize stale schemas.
    _CACHE_SCHEMA = "v10"  # v10: SBI FD interest accrued; v9: tradebook qty/price/kind; v8: Coin purchase-confirmation + redemption mails; v7: 2021 holdings layout; v6/v5: as-on dates; v4: demat movements = balance deltas; v3: contract-note text layouts; v2: ledger fields

    def __init__(self, pdf_password: str = ""):
        self.pdf_password = pdf_password

    def parse(self, path: Path) -> ParseResult:
        raise NotImplementedError

    # ── Whole-result cache ───────────────────────────────────────────────────
    def parse_cached(self, path: Path) -> ParseResult:
        """
        Same as `parse(path)`, but caches the resulting ParseResult on disk
        keyed by sha1(file_bytes + parser_class + schema_version). Re-running
        `sync/historical` over the same PDFs skips pdfplumber + LLM entirely
        and returns the prior ParseResult in <1 ms.

        Disable with env var PARSE_RESULT_CACHE=0.
        """
        import os, hashlib, pickle, logging
        log = logging.getLogger(__name__)

        if os.getenv("PARSE_RESULT_CACHE", "1") == "0":
            return self.parse(path)

        try:
            file_bytes = path.read_bytes()
        except Exception:
            return self.parse(path)

        h = hashlib.sha1()
        h.update(file_bytes)
        h.update(b"\x00")
        h.update(type(self).__name__.encode())
        h.update(b"\x00")
        h.update(self._CACHE_SCHEMA.encode())
        key = h.hexdigest()

        cache_dir = Path(os.getenv("PARSE_RESULT_CACHE_DIR", "data/cache/parsed"))
        cache_file = cache_dir / f"{key}.pkl"

        if cache_file.exists():
            try:
                result = pickle.loads(cache_file.read_bytes())
                log.debug("ParseResult cache HIT %s -> %s", path.name, key[:8])
                return result
            except Exception as exc:
                log.debug("ParseResult cache read failed (%s) — re-parsing", exc)

        result = self.parse(path)

        try:
            cache_dir.mkdir(parents=True, exist_ok=True)
            cache_file.write_bytes(pickle.dumps(result))
        except Exception as exc:
            log.debug("ParseResult cache write failed: %s", exc)

        return result

    def _open_pdf(self, path: Path):
        """Open a pdfplumber PDF, trying with password first then without."""
        import pdfplumber
        if self.pdf_password:
            try:
                return pdfplumber.open(path, password=self.pdf_password)
            except Exception:
                pass  # wrong password — fall through and try without
        try:
            return pdfplumber.open(path)
        except Exception as exc:
            # Re-raise with a clearer message so the router log is informative
            raise RuntimeError(
                f"Cannot open {path.name} — likely password-protected with wrong/missing password: {exc}"
            ) from exc

    def _extract_text_cached(self, path: Path) -> str:
        """
        Return the full text of a PDF, cached as a `.txt` sidecar next to the
        original file. pdfplumber is the slowest step in most parsers (~0.5–2 s/page);
        re-running sync over already-seen statements becomes near-instant after the
        first pass.

        Cache invalidates automatically if the source PDF's mtime is newer than
        the sidecar's. Disable with env var PDF_TEXT_CACHE=0.
        """
        import os, logging
        log = logging.getLogger(__name__)
        cache_enabled = os.getenv("PDF_TEXT_CACHE", "1") != "0"
        sidecar = path.with_suffix(path.suffix + ".txt.cache")

        if cache_enabled and sidecar.exists():
            try:
                if sidecar.stat().st_mtime >= path.stat().st_mtime:
                    return sidecar.read_text(encoding="utf-8", errors="replace")
            except Exception:
                pass

        # Extract fresh
        with self._open_pdf(path) as pdf:
            text = "\n".join((p.extract_text() or "") for p in pdf.pages)

        if cache_enabled:
            try:
                sidecar.write_text(text, encoding="utf-8")
            except Exception as exc:
                log.debug("PDF text cache write failed: %s", exc)
        return text

    # ── LLM fallback ─────────────────────────────────────────────────────────

    def _llm_extract(self, doc_type: str, section_text: str) -> list[dict]:
        """
        Call the local Ollama LLM extractor for a document section.
        Returns [] gracefully when Ollama is unavailable or extraction yields nothing.
        Subclasses call this when regex extraction returns empty but the section exists.
        """
        try:
            from app.ingestion import llm_extractor
            return llm_extractor.extract(doc_type, section_text, self.institution)
        except Exception as exc:
            import logging
            logging.getLogger(__name__).warning(
                "LLM extract(%s, %s) unexpected error: %s", self.institution, doc_type, exc
            )
            return []

    # ── helpers ──────────────────────────────────────────────────────────────

    @staticmethod
    def _to_decimal(value) -> Optional[Decimal]:
        if value is None:
            return None
        try:
            cleaned = str(value).replace(",", "").replace(" ", "").strip()
            if not cleaned or cleaned in ("-", ""):
                return None
            return Decimal(cleaned)
        except Exception:
            return None

    @staticmethod
    def _parse_date(value: str, formats: list[str]) -> Optional[date]:
        from datetime import datetime
        for fmt in formats:
            try:
                return datetime.strptime(value.strip(), fmt).date()
            except (ValueError, AttributeError):
                pass
        return None
