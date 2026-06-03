"""
LLM-based structured extraction for PDF sections via Ollama (fully local, private).

Used as a fallback when a regex parser returns empty results but the relevant
section clearly exists in the document — e.g. an SBI statement that has a
"TDR AND STDR" block but whose layout changed so the regex matched nothing.

The LLM reads the raw section text and returns structured JSON directly, making
format changes self-healing without requiring regex maintenance.

Setup (one-time):
    brew install ollama            # macOS
    ollama pull llama3.2           # fast, ~2 GB; works for most extractions
    ollama pull qwen2.5:7b         # better JSON accuracy (~4 GB, recommended)
    ollama serve

Environment variables (all optional):
    OLLAMA_HOST         default: http://ollama:11434  (in-Docker service name)
                        set to http://host.docker.internal:11434 to use host Ollama instead
    OLLAMA_PARSE_MODEL  default: value of OLLAMA_MODEL, else "llama3.2"
                        recommend "qwen2.5:7b" for higher accuracy
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import urllib.request
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

OLLAMA_HOST        = os.getenv("OLLAMA_HOST",        "http://ollama:11434")
OLLAMA_PARSE_MODEL = os.getenv("OLLAMA_PARSE_MODEL",  os.getenv("OLLAMA_MODEL", "llama3.2:1b"))

# Truncate section text sent to LLM — llama3.2 context window is 128K tokens;
# 16 000 chars covers even the longest Indian bank FD/holdings sections comfortably.
_MAX_CHARS = 16000

# ── Performance tuning ────────────────────────────────────────────────────────
# Cap the number of tokens the LLM is allowed to emit per doc_type.
# Generating 3000 tokens on CPU is ~20-30 s wasted when the answer is one number.
_NUM_PREDICT: dict[str, int] = {
    "bank_ppf":          64,     # one number
    "bank_fd":           1500,   # ~10 FDs worst case
    "equity_holdings":   2000,
    "mf_holdings":       2000,
    "bank_transactions": 3000,
}
_DEFAULT_NUM_PREDICT = 1500

# Disk cache for LLM responses — keyed by sha1(model + prompt + filtered_text).
# Idempotent re-runs of `sync/historical` no longer pay LLM cost twice.
_CACHE_DIR = Path(os.getenv("LLM_CACHE_DIR", "data/cache/llm"))
_CACHE_ENABLED = os.getenv("LLM_CACHE", "1") != "0"


# ── Extraction prompts ────────────────────────────────────────────────────────
# Each entry is keyed by doc_type string and describes the expected JSON schema.
# Ollama is called with format="json" so output is always valid JSON, though
# individual fields may be null when the model cannot find them.

_PROMPTS: dict[str, str] = {

    # ── Bank: fixed / term deposits ───────────────────────────────────────────
    "bank_fd": """\
Extract all fixed deposit / term deposit records from this Indian bank statement text.
Return JSON with a "records" array. Each element must have these fields:
  last4           (string)  last 4 characters of the FD account number
  open_date       (string)  opening/start date in YYYY-MM-DD format
  principal       (number)  deposited principal — plain number, no commas or symbols
  roi             (number)  annual interest rate as percent (e.g. 6.25, not 0.0625)
  maturity_amount (number)  final maturity/redemption value — plain number
  maturity_date   (string)  maturity date in YYYY-MM-DD format

IMPORTANT date rules (Indian bank statements use DD-MM-YY format):
- "11-10-24" = day 11, month 10 (Oct), year 2024 → "2024-10-11"
- "02-03-25" = day 02, month 03 (Mar), year 2025 → "2025-03-02"
- Always interpret as DD-MM-YY or DD-MM-YYYY, never as MM-DD-YY

Other rules:
- Strip commas and currency symbols from all numbers
- Use null for any field that cannot be determined
- Skip header rows, column label rows, and grand-total rows
- Extract ALL records — do not stop after the first one
- Return {"records": []} if no FD records are present

Example with 2 records:
{"records": [
  {"last4": "6925", "open_date": "2024-10-11", "principal": 140000, "roi": 6.25, "maturity_amount": 139970.0, "maturity_date": "2026-10-11"},
  {"last4": "1476", "open_date": "2025-03-02", "principal": 145420, "roi": 6.25, "maturity_amount": 145420.0, "maturity_date": "2027-03-02"}
]}
""",

    # ── Bank: PPF balance ─────────────────────────────────────────────────────
    "bank_ppf": """\
Extract the PPF (Public Provident Fund) account balance from this bank statement text.
Return JSON: {"balance": <number>}
The balance must be a plain number with no commas or ₹ symbol (e.g. 1787131.00).
If no PPF balance is found return: {"balance": null}
""",

    # ── Equity / demat holdings ───────────────────────────────────────────────
    "equity_holdings": """\
Extract all equity / stock holdings from this broker or demat statement text.
Return JSON with a "records" array. Each element must have:
  isin          (string)  12-character ISIN code starting with IN (e.g. INE002A01018)
  symbol        (string)  stock ticker or company name
  quantity      (number)  shares / units currently held — plain number
  avg_cost      (number)  average purchase price per share, or null
  current_price (number)  current market price per share, or null
  current_value (number)  total current market value — plain number

Rules:
- Strip commas from all numbers
- ISIN is always 12 characters (2 letters + 10 alphanumeric)
- Skip zero-quantity rows and header/total rows
- Return {"records": []} if no equity holdings are found
""",

    # ── Mutual fund holdings ──────────────────────────────────────────────────
    "mf_holdings": """\
Extract all mutual fund holdings from this statement text.
Return JSON with a "records" array. Each element must have:
  scheme_name    (string)  full fund / scheme name
  isin           (string)  ISIN code if present, else null
  folio          (string)  folio number if present, else null
  units          (number)  units held — plain number
  nav            (number)  current NAV (net asset value) per unit — plain number
  current_value  (number)  total current value — plain number
  invested_value (number)  total invested / cost value if available, else null

Rules:
- Strip commas from numbers
- Return {"records": []} if no mutual fund holdings are found
""",

    # ── Generic bank transactions (last-resort fallback) ─────────────────────
    "bank_transactions": """\
Extract all bank transactions from this statement text.
Return JSON with a "records" array. Each element must have:
  date        (string)  transaction date in YYYY-MM-DD format
  description (string)  transaction narration / description
  amount      (number)  transaction amount — plain positive number
  type        (string)  "credit" if money came in, "debit" if money went out
  balance     (number)  running balance after transaction, or null

Rules:
- Convert all dates to YYYY-MM-DD
- Strip commas and currency symbols from numbers
- Skip header rows and opening/closing balance summary rows
- Return {"records": []} if no transactions are found
""",
}


# ── Pre-filters ───────────────────────────────────────────────────────────────
# Reduce section text to only the lines that carry data before sending to LLM.
# This keeps prompt+input well within the context window and halves inference time.

def _prefilter(doc_type: str, text: str) -> str:
    """Return a trimmed version of text containing only the data-bearing lines."""
    lines = text.splitlines()

    if doc_type == "bank_fd":
        # Keep TERM DEPOSIT lines — each is one FD record; everything else is headers/notes.
        data_lines = [l for l in lines if "TERM DEPOSIT" in l.upper() and any(c.isdigit() for c in l)]
        if data_lines:
            return "\n".join(data_lines)

    if doc_type == "equity_holdings":
        # Keep lines that start with a 12-char ISIN-looking token or contain quantities+values.
        import re as _re
        data_lines = [l for l in lines if _re.match(r'\s*[A-Z]{2}[A-Z0-9]{9}[0-9]\s', l)]
        if data_lines:
            return "\n".join(data_lines)

    # For other types (bank_ppf, mf_holdings, bank_transactions) pass full text.
    return text[:_MAX_CHARS]


# ── Core call ─────────────────────────────────────────────────────────────────

def _cache_key(prompt: str, section_text: str) -> str:
    h = hashlib.sha1()
    h.update(OLLAMA_PARSE_MODEL.encode())
    h.update(b"\x00")
    h.update(prompt.encode())
    h.update(b"\x00")
    h.update(section_text.encode())
    return h.hexdigest()


def _cache_get(key: str) -> dict[str, Any] | None:
    if not _CACHE_ENABLED:
        return None
    f = _CACHE_DIR / f"{key}.json"
    if not f.exists():
        return None
    try:
        return json.loads(f.read_text())
    except Exception:
        return None


def _cache_put(key: str, value: dict[str, Any]) -> None:
    if not _CACHE_ENABLED:
        return
    try:
        _CACHE_DIR.mkdir(parents=True, exist_ok=True)
        (_CACHE_DIR / f"{key}.json").write_text(json.dumps(value, ensure_ascii=False))
    except Exception as exc:
        logger.debug("LLM cache write failed: %s", exc)


def _call_ollama(prompt: str, section_text: str, num_predict: int = _DEFAULT_NUM_PREDICT) -> dict[str, Any] | None:
    """
    POST to Ollama /api/generate with JSON output mode.
    Returns a parsed dict, or None if Ollama is unreachable / call fails.
    Results are cached on disk by sha1(model+prompt+text) so re-runs are free.
    """
    ckey = _cache_key(prompt, section_text)
    cached = _cache_get(ckey)
    if cached is not None:
        logger.debug("LLM cache HIT (%s)", ckey[:8])
        return cached

    body = json.dumps({
        "model":   OLLAMA_PARSE_MODEL,
        "prompt":  f"{prompt}\n\nTEXT TO EXTRACT FROM:\n{section_text}",
        "stream":  False,
        "format":  "json",
        # keep_alive=-1 holds the model in RAM across calls (avoids ~10s cold start
        # every time, in case the server-side OLLAMA_KEEP_ALIVE wasn't set).
        "keep_alive": -1,
        "options": {"temperature": 0, "num_predict": num_predict},
    }).encode()

    try:
        req = urllib.request.Request(
            f"{OLLAMA_HOST}/api/generate",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        # 5-minute timeout — CPU inference on a full section takes 1-3 min on modest hardware.
        with urllib.request.urlopen(req, timeout=300) as resp:
            raw = json.loads(resp.read())
        parsed = json.loads(raw.get("response", "{}"))
        _cache_put(ckey, parsed)
        return parsed
    except OSError:
        logger.debug("Ollama not reachable at %s — LLM extraction skipped", OLLAMA_HOST)
        return None
    except json.JSONDecodeError as exc:
        logger.warning("Ollama returned non-JSON response: %s", exc)
        return None
    except Exception as exc:
        logger.warning("Ollama call failed (%s): %s", OLLAMA_PARSE_MODEL, exc)
        return None


# ── Public API ────────────────────────────────────────────────────────────────

def extract(doc_type: str, section_text: str, institution: str = "") -> list[dict]:
    """
    Extract structured records from a PDF section via local Ollama LLM.

    Returns a list of raw dicts whose keys depend on doc_type (see _PROMPTS).
    Returns [] gracefully when Ollama is unavailable or extraction yields nothing.

    doc_type values:
        "bank_fd"           → FD/TD records from any bank statement
        "bank_ppf"          → PPF balance  (returns [{"balance": N}])
        "equity_holdings"   → equity/demat holdings
        "mf_holdings"       → mutual fund holdings
        "bank_transactions" → transactions (last-resort fallback)
    """
    prompt = _PROMPTS.get(doc_type)
    if not prompt:
        logger.warning("llm_extractor: unknown doc_type %r", doc_type)
        return []

    if not section_text.strip():
        return []

    tag = f"[{institution}] " if institution else ""
    filtered = _prefilter(doc_type, section_text)

    # Regex fast-path: bank_ppf is just a single balance number. Try a cheap
    # regex first; only call the LLM if it fails. Saves ~30 s per PPF section.
    if doc_type == "bank_ppf":
        import re as _re
        # Look for "PPF" or "Public Provident" within ~200 chars of a balance-looking number.
        m = _re.search(
            r"(?:PPF|Public\s+Provident)[^\n]{0,200}?([\d,]+\.\d{2})",
            section_text, _re.IGNORECASE | _re.DOTALL,
        )
        if m:
            try:
                bal = float(m.group(1).replace(",", ""))
                logger.info("%sPPF balance via regex fast-path: %.2f", tag, bal)
                return [{"balance": bal}]
            except ValueError:
                pass

    logger.info("%sLLM extract: type=%s  model=%s  chars=%d→%d",
                tag, doc_type, OLLAMA_PARSE_MODEL, len(section_text), len(filtered))

    result = _call_ollama(prompt, filtered, _NUM_PREDICT.get(doc_type, _DEFAULT_NUM_PREDICT))
    if result is None:
        return []

    # bank_ppf returns a single scalar — normalise to a 1-element list
    if doc_type == "bank_ppf":
        bal = result.get("balance")
        return [{"balance": float(bal)}] if bal is not None else []

    records = result.get("records")
    if not isinstance(records, list):
        logger.warning("LLM extraction: 'records' field missing or not a list in response")
        return []

    logger.info("%sLLM extracted %d records (type=%s)", tag, len(records), doc_type)
    return records


def is_available() -> bool:
    """Quick probe — returns True if Ollama is reachable right now."""
    try:
        urllib.request.urlopen(f"{OLLAMA_HOST}/api/tags", timeout=3)
        return True
    except OSError:
        return False
