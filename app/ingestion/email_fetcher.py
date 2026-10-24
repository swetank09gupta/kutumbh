"""
Multi-account email fetcher.
Connects to each configured Gmail/Yahoo inbox, finds financial statement
emails, downloads attachments to data/raw/<owner>/ and returns metadata.
"""
from __future__ import annotations

import email
import imaplib
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional


def _worker_count(env_var: str, cpu_ratio: float = 1.0, minimum: int = 1, cap: int = 8) -> int:
    """
    Return a sensible worker count for this machine.

    Priority: explicit env override > cpu-based heuristic > minimum.
    cpu_ratio > 1.0 is appropriate for pure I/O-bound work (network waits);
    cpu_ratio = 1.0 is appropriate for CPU/IO-mixed work (PDF parsing).
    cap is a hard ceiling to avoid overwhelming low-memory machines or
    triggering rate-limits on remote services regardless of CPU count.
    Falls back to minimum (1) when os.cpu_count() is unavailable (containers,
    restricted environments).
    """
    env_val = os.environ.get(env_var, "").strip()
    if env_val:
        try:
            return max(minimum, int(env_val))
        except ValueError:
            pass
    cpus = os.cpu_count() or 1
    return max(minimum, min(cap, round(cpus * cpu_ratio)))

from app.config import settings

logger = logging.getLogger(__name__)

RAW_DIR = Path("data/raw")


# ── Known financial senders ──────────────────────────────────────────────────
# Maps partial sender address → (institution, account_type hint)
KNOWN_SENDERS: dict[str, dict] = {
    # ── HDFC Bank ─────────────────────────────────────────────────────────────
    "hdfcbank.com":       {"institution": "HDFC", "account_type": "bank"},
    "hdfcbank.net":       {"institution": "HDFC", "account_type": "bank"},
    "hdfcbank.bank.in":   {"institution": "HDFC", "account_type": "bank"},

    # ── SBI ────────────────────────────────────────────────────────────────────
    "sbiyono@sbi.co.in":                {"institution": "SBI",  "account_type": "bank"},
    "onlinesbi@sbi.co.in":              {"institution": "SBI",  "account_type": "bank"},
    "donotreply@sbi.co.in":             {"institution": "SBI",  "account_type": "bank"},
    "noreply@sbi.co.in":                {"institution": "SBI",  "account_type": "bank"},
    "alerts@sbi.co.in":                 {"institution": "SBI",  "account_type": "bank"},
    "statements@sbi.co.in":             {"institution": "SBI",  "account_type": "bank"},
    "sbi.co.in":                        {"institution": "SBI",  "account_type": "bank"},
    # Monthly consolidated statements (newer SBI mailer domain)
    "cbssbi.cas@alerts.sbi.bank.in":    {"institution": "SBI",  "account_type": "bank"},
    "alerts.sbi.bank.in":               {"institution": "SBI",  "account_type": "bank"},

    # ── Union Bank of India ─────────────────────────────────────────────────────
    # Union Bank uses multiple domains — match any address containing "unionbank"
    "unionbankofindia.com":             {"institution": "Union Bank", "account_type": "bank"},
    "unionbank.co.in":                  {"institution": "Union Bank", "account_type": "bank"},
    "ubi.bank.in":                      {"institution": "Union Bank", "account_type": "bank"},   # since 2026: noreplyunionbank@ubi.bank.in
    "donotreply@unionbankofindia.com":  {"institution": "Union Bank", "account_type": "bank"},
    "alerts@unionbankofindia.com":      {"institution": "Union Bank", "account_type": "bank"},
    "statements@unionbankofindia.com":  {"institution": "Union Bank", "account_type": "bank"},
    "noreply@unionbankofindia.com":     {"institution": "Union Bank", "account_type": "bank"},
    "unionbank":                        {"institution": "Union Bank", "account_type": "bank"},

    # ── Axis Bank ─────────────────────────────────────────────────────────────
    "axisbank.com":  {"institution": "Axis", "account_type": "bank"},
    # Since 2026 statements come from axis.bank.in. Exact address, not the domain: Straight2axis@axis.bank.in sends payment advices.
    "statements@axis.bank.in": {"institution": "Axis", "account_type": "bank"},

    # ── Zerodha ────────────────────────────────────────────────────────────────
    # Monthly demat statements come from reportsmailer.zerodha.net, not zerodha.com
    "no-reply@zerodha.com":             {"institution": "Zerodha", "account_type": "stocks"},
    "backoffice@zerodha.com":           {"institution": "Zerodha", "account_type": "stocks"},
    "statements@zerodha.com":           {"institution": "Zerodha", "account_type": "stocks"},
    "reportsmailer.zerodha.net":        {"institution": "Zerodha", "account_type": "stocks"},
    # Coin MF buy/redeem allotment emails (HTML body — no PDF attachment).
    # MUST be listed before the generic "zerodha.net"/"zerodha.com" rules below —
    # _match_sender iterates in insertion order and stops at the first substring hit.
    "noreply-coin@qmailer.zerodha.net": {"institution": "Zerodha Coin", "account_type": "mutual_fund"},
    "qmailer.zerodha.net":              {"institution": "Zerodha Coin", "account_type": "mutual_fund"},
    "noreply@coinmailer.zerodha.net":   {"institution": "Zerodha Coin", "account_type": "mutual_fund"},
    "coinmailer.zerodha.net":           {"institution": "Zerodha Coin", "account_type": "mutual_fund"},
    "alerts@mailer.zerodha.com":        {"institution": "Zerodha Coin", "account_type": "mutual_fund"},
    "zerodha.net":                      {"institution": "Zerodha", "account_type": "stocks"},

    # ── CAMS (mutual funds) ────────────────────────────────────────────────────
    "mfs@camsonline.com":               {"institution": "CAMS", "account_type": "mutual_fund"},
    "caims@camsonline.com":             {"institution": "CAMS", "account_type": "mutual_fund"},
    "contact@camsonline.com":           {"institution": "CAMS", "account_type": "mutual_fund"},
    "camsonline.com":                   {"institution": "CAMS", "account_type": "mutual_fund"},

    # ── KFintech (mutual funds) ────────────────────────────────────────────────
    "kfin@kfintech.com":                {"institution": "KFintech", "account_type": "mutual_fund"},
    "info@kfintech.com":                {"institution": "KFintech", "account_type": "mutual_fund"},
    "kfintech.com":                     {"institution": "KFintech", "account_type": "mutual_fund"},

    # ── CDSL (demat / CAS statement) ──────────────────────────────────────────
    "eCAS@cdslstatement.com":           {"institution": "CDSL", "account_type": "demat"},
    "cdslstatement.com":                {"institution": "CDSL", "account_type": "demat"},
    # "Transactions In Your Demat Account": per-ISIN credits/debits within a day of settlement.
    # Body-only mails; used by the instrument ledger to see units that arrived after the last statement.
    "services@cdslindia.co.in":         {"institution": "CDSL Alert", "account_type": "demat"},
}

ATTACHMENT_EXTENSIONS = {".pdf", ".csv", ".xlsx", ".xls"}


@dataclass
class FetchedAttachment:
    owner_id: str
    institution: str
    account_type: str
    sender: str
    subject: str
    email_date: str
    file_path: Path
    filename: str
    pdf_password: str = ""   # resolved from email body or config


def _extract_pdf_password_from_body(msg) -> str:
    """
    Many Indian banks embed the PDF password in the email body.
    Try to find it before falling back to configured passwords.
    Common patterns:
      - "Password to open ... is XXXXX"
      - "password is XXXXX"
      - "Password: XXXXX"
      - "open the attached ... with password XXXXX"
    Returns the password string, or "" if not found.
    """
    import re
    body = ""
    if msg.is_multipart():
        for part in msg.walk():
            ct = part.get_content_type()
            if ct in ("text/plain", "text/html"):
                try:
                    body += part.get_payload(decode=True).decode(errors="ignore")
                except Exception:
                    pass
    else:
        try:
            body = msg.get_payload(decode=True).decode(errors="ignore")
        except Exception:
            pass

    # Strip HTML tags for easier matching
    body_text = re.sub(r"<[^>]+>", " ", body)

    patterns = [
        r"[Pp]assword\s+(?:to open\s+(?:the\s+)?(?:attached\s+)?(?:document|file|PDF)\s+)?(?:is|:)\s*[\"']?([A-Za-z0-9@._\-]{4,20})[\"']?",
        r"open\s+(?:the\s+)?(?:attached\s+)?(?:document|file|PDF)\s+(?:with|using)\s+(?:the\s+)?password\s+[\"']?([A-Za-z0-9@._\-]{4,20})[\"']?",
        r"[Pp]DF\s+[Pp]assword\s*[:\-]\s*[\"']?([A-Za-z0-9@._\-]{4,20})[\"']?",
        r"[Pp]assword\s*:\s*[\"']?([A-Za-z0-9@._\-]{4,20})[\"']?",
    ]
    # Words that are instructions, NOT actual passwords (e.g. "...is first four chars of name")
    SKIP_WORDS = {
        "the", "your", "our", "this", "that", "with", "open", "below", "above",
        "first", "four", "last", "characters", "letters", "digits", "name",
        "uppercase", "capital", "lower", "number", "account", "date", "birth",
        "mobile", "phone", "email", "please", "note", "only", "attached",
    }
    for pat in patterns:
        m = re.search(pat, body_text)
        if m:
            candidate = m.group(1).strip()
            if candidate.lower() in SKIP_WORDS:
                continue
            if len(candidate) < 4:
                continue
            # All real Indian bank passwords contain at least one digit
            # (DOB parts, PAN digits, account digits).
            # Reject pure-alpha candidates — they're instructions, not passwords.
            if not any(c.isdigit() for c in candidate):
                continue
            logger.debug("Extracted PDF password from email body: %s…", candidate[:3])
            return candidate
    return ""


def _derive_pdf_password(owner_id: str, institution: str) -> str:
    """
    Derive the PDF open password from the owner's personal info.
    Each Indian institution follows a published, fixed formula.

    Formula reference:
      HDFC        : name[:4].upper() + DD + MM              e.g. SWET1503
      SBI         : name[:4].upper() + DD + MM + YYYY       e.g. SWET15031990
      Axis        : name[:4].upper() + DD + MM               e.g. AKAN2808
      Union Bank  : name[:4].upper() + DD + MM              e.g. SWET1503
                    (fallback: name[:4] + last-4 mobile)
      Zerodha     : PAN uppercase                           e.g. ABCDE1234F
      CAMS/KFin   : PAN uppercase                           e.g. ABCDE1234F
      CDSL        : PAN uppercase                           e.g. ABCDE1234F
    """
    from app.config import settings
    profile = settings.get_owner_profile(owner_id)
    if not profile:
        return ""

    name   = profile.get("name", "")
    dob    = profile.get("dob", "")     # YYYY-MM-DD
    pan    = profile.get("pan", "")
    mobile = profile.get("mobile", "")

    # Name prefix: first 4 letters of FIRST word, or full name stripped of spaces
    name_clean = name.replace(" ", "")
    name4 = name_clean[:4].upper()

    # DOB parts
    dob_dd   = dob[8:10]  if len(dob) >= 10 else ""
    dob_mm   = dob[5:7]   if len(dob) >= 7  else ""
    dob_yyyy = dob[0:4]   if len(dob) >= 4  else ""

    inst = institution.lower()

    if "hdfc" in inst:
        # NetBanking-downloaded PDFs use a CIF-based password stored explicitly in config.
        # Smart Statement server auth uses a separate formula (see _derive_hdfc_smart_password).
        nb_pwd = profile.get("hdfc_pdf_password", "")
        if nb_pwd:
            return nb_pwd
        if name4 and dob_dd and dob_mm:
            return name4 + dob_dd + dob_mm
    elif "sbi" in inst:
        # SBI e-statement (savings/current/FD): last5_mobile + DDMMYY (2-digit year)
        # Source: SBI email body "password is last five digits of mobile + DOB in DDMMYY format"
        dob_yy = dob[2:4] if len(dob) >= 4 else ""  # 2-digit year
        if mobile and len(mobile) >= 5 and dob_dd and dob_mm and dob_yy:
            return mobile[-5:] + dob_dd + dob_mm + dob_yy
    elif "axis" in inst:
        # Axis: NAME4 + DDMM  (first 4 letters of name + DDMM of DOB)
        if name4 and dob_dd and dob_mm:
            return name4 + dob_dd + dob_mm
    elif "union" in inst:
        # Union Bank: NAME4 + DDMM (same as HDFC pattern)
        if name4 and dob_dd and dob_mm:
            return name4 + dob_dd + dob_mm
    elif "zerodha" in inst:
        return pan
    elif "cams" in inst or "kfintech" in inst or "cdsl" in inst:
        return pan

    return ""


def _all_pdf_passwords(institution: str, primary_owner_id: str) -> list[str]:
    """
    Return all distinct candidate passwords for an institution, starting with
    the primary owner then falling back to every other owner in accounts.yml.
    Used when a PDF downloaded from one owner's email is password-protected
    with a different owner's personal details (e.g. user's bank statement
    arrives in wife's inbox).
    """
    from app.config import settings
    import yaml, os
    from pathlib import Path

    cfg_path = Path("config/accounts.yml")
    try:
        raw = cfg_path.read_text()
        raw = os.path.expandvars(raw)
        data = yaml.safe_load(raw)
        all_owner_ids = list({o["id"] for o in data.get("owners", [])})
    except Exception:
        all_owner_ids = [primary_owner_id]

    # Primary owner first, then others
    ordered = [primary_owner_id] + [oid for oid in all_owner_ids if oid != primary_owner_id]

    seen: set[str] = set()
    passwords: list[str] = []
    for oid in ordered:
        pwd = _derive_pdf_password(oid, institution)
        if pwd and pwd not in seen:
            seen.add(pwd)
            passwords.append(pwd)
        # Documents whose password is an account number, not a name+DOB formula (kept in the private config):
        #   Union Bank statements of 2020-21: the 15-digit account number (the bank's own e-mail says so)
        #   HDFC home-loan tax documents (IT_PROV / IT_FINAL): the loan account number
        profile = settings.get_owner_profile(oid) or {}
        for key, bank in (("union_bank_account_no", "union"), ("hdfc_loan_account_no", "hdfc")):
            extra = str(profile.get(key) or "").strip()
            if extra and bank in institution.lower() and extra not in seen:
                seen.add(extra)
                passwords.append(extra)
        # HDFC: when a CIF password is configured it wins as primary, but some
        # email-delivered statements use the name+DOB formula instead.  Add
        # the formula as an extra candidate so both are tried.
        if "hdfc" in institution.lower():
            name_pwd = _derive_hdfc_smart_password(oid)
            if name_pwd and name_pwd not in seen:
                seen.add(name_pwd)
                passwords.append(name_pwd)
    return passwords


def _derive_hdfc_smart_password(owner_id: str) -> str:
    """
    HDFC Smart Statement server auth password (used in _hdfc_js_encrypt call).
    Always name[:4].upper() + DDMM — independent of hdfc_pdf_password in config.
    """
    from app.config import settings
    profile = settings.get_owner_profile(owner_id)
    name4 = profile.get("name", "").replace(" ", "")[:4].upper()
    dob   = profile.get("dob", "")
    if name4 and len(dob) >= 10:
        return name4 + dob[8:10] + dob[5:7]
    return ""


def _resolve_pdf_password(owner_id: str, institution: str, body_password: str) -> str:
    """
    Priority:
      1. Password found in email body (bank included it explicitly)
      2. Derived from owner's personal info (dob, pan) via institution formula
      3. Empty — pdfplumber will try without password (works for unprotected files)
    """
    if body_password:
        logger.debug("Using password from email body for %s / %s", owner_id, institution)
        return body_password

    derived = _derive_pdf_password(owner_id, institution)
    if derived:
        logger.debug("Derived PDF password for %s / %s", owner_id, institution)
    return derived


def _match_sender(from_addr: str) -> Optional[dict]:
    """Return institution metadata if the sender is a known financial institution."""
    from_lower = from_addr.lower()
    for pattern, meta in KNOWN_SENDERS.items():
        if pattern in from_lower:
            return meta
    return None


def _safe_filename(name: str) -> str:
    return "".join(c if c.isalnum() or c in "._-" else "_" for c in name)


def _extract_hdfc_smart_link(msg) -> str:
    """
    HDFC 'Smart Statement' emails embed a download URL in the HTML body instead of
    attaching a PDF.  Extract the statement download URL.

    Known patterns (HDFC has changed subdomains over the years):
      https://smartstatements.hdfcbank.com/HDFCRestFulService/GetStatement.jsp?jobkey=<token>
      https://netbanking.hdfcbank.com/...
      https://estatements.hdfcbank.com/...
    """
    import re
    body = ""
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() in ("text/html", "text/plain"):
                try:
                    body += part.get_payload(decode=True).decode(errors="ignore")
                except Exception:
                    pass
    else:
        try:
            body = msg.get_payload(decode=True).decode(errors="ignore")
        except Exception:
            pass

    # Try broad HDFC URL patterns — matches both hdfcbank.com and hdfc.bank.in domains
    patterns = [
        r"https?://[a-z0-9.-]*hdfcbank\.com/[^\s\"'<>]+GetStatement[^\s\"'<>]*",
        r"https?://[a-z0-9.-]*hdfc\.bank\.in/[^\s\"'<>]+GetStatement[^\s\"'<>]*",
        r"https?://[a-z0-9.-]*hdfcbank\.com/[^\s\"'<>]*jobkey=[A-Za-z0-9]+",
        r"https?://[a-z0-9.-]*hdfc\.bank\.in/[^\s\"'<>]*jobkey=[A-Za-z0-9]+",
        r"https?://[a-z0-9.-]*hdfcbank\.com/[^\s\"'<>]*statement[^\s\"'<>]*",
        r"https?://[a-z0-9.-]*hdfc\.bank\.in/[^\s\"'<>]*statement[^\s\"'<>]*",
    ]
    import html as _html
    for pattern in patterns:
        m = re.search(pattern, body, re.IGNORECASE)
        if m:
            return _html.unescape(m.group(0).rstrip(".,;)"))
    return ""


def _is_hdfc_statement_email(subject: str) -> bool:
    """Return True for HDFC account-statement emails (Smart Link or attachment)."""
    if not subject:
        return False
    subj = subject.lower()
    return (
        "email account statement" in subj and "hdfc" in subj
        or "account statement" in subj and "hdfc bank" in subj
        or "e-statement" in subj and "hdfc" in subj
        or "estatement" in subj and "hdfc" in subj
    )


def _hdfc_js_encrypt(plaintext: str) -> str:
    """
    Replicate HDFC Smart Statement encrypt.js algorithm.

    Before POSTing the password, the browser:
      1. GETs a nonce token from CRSGetToken?jobkey=...
      2. Concatenates: token + password
      3. Encrypts the concatenation with this algorithm
      4. POSTs the encrypted value as the 'pwd' field

    Algorithm: XOR-based stream cipher using key "toUpperCase", seeded with
    a random byte. Output is uppercased hex, starting with the 2-char hex seed.
    """
    import random
    KEY = "toUpperCase"
    key_len = len(KEY)
    seed = int((random.random() * 10000) % 255) + 1
    result = format(seed, "02x")
    key_idx = -1
    for ch in plaintext:
        char_result = int((ord(ch) + seed) % 255)
        if key_idx < key_len - 1:
            key_idx += 1
        else:
            key_idx = 0
        char_result ^= ord(KEY[key_idx])
        result += format(char_result, "02x")
        seed = char_result
    return result.upper()


def _download_hdfc_smart_pdf(
    url: str,
    owner_id: str,
    subject: str,
    date_str: str,
    save_dir: Path,
    pdf_password: str = "",
) -> Path | None:
    """
    Download an HDFC Smart Statement and save the decrypted HTML to disk.

    HDFC Smart Statement flow (smartstatements.hdfc.bank.in):
      Step 1: GET GetStatement.jsp → HTML form; extract ke, seqence hidden fields
      Step 2: GET CRSGetToken?jobkey=ke → nonce token string
      Step 3: Encrypt (token + password) with _hdfc_js_encrypt()
      Step 4: POST ke + seqence + encrypted_pwd → returns AES-encrypted HTML
      Step 5: Decrypt HTML with AES-ECB using embedded key from Data() call
      Step 6: Save decrypted statement HTML to HDFC_<acct>_<period>.html

    The .html file is then parsed by HdfcParser._parse_html().
    """
    import re
    import base64
    import requests
    import urllib3
    urllib3.disable_warnings()

    # ── Build destination filename ────────────────────────────────────────────
    acct_m  = re.search(r"\*{2,3}(\d{4})", subject)
    acct4   = acct_m.group(1) if acct_m else "XXXX"

    period_m = re.search(
        r"(\d{2}-\w{3}-\d{4})\s+TO\s+(\d{2}-\w{3}-\d{4})", subject, re.IGNORECASE
    )
    if period_m:
        from_date = period_m.group(1).replace("-", "")
        to_date   = period_m.group(2).replace("-", "")
        date_tag  = f"{from_date}_{to_date}"
    else:
        date_tag = date_str[:11].replace(" ", "_") if date_str else "unknown"

    filename = f"HDFC_{acct4}_{date_tag}.html"
    dest     = save_dir / filename

    if dest.exists():
        logger.info("HDFC smart statement already present: %s", filename)
        return dest

    # Extract base URL from the GetStatement.jsp URL
    base_m = re.match(r"(https?://[^/]+/[^?]+/)", url)
    if not base_m:
        logger.error("Cannot parse HDFC base URL from: %s", url[:80])
        return None
    base_url = base_m.group(1).rstrip("/")

    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                      "AppleWebKit/537.36 (KHTML, like Gecko) "
                      "Chrome/124.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
        "Accept-Language": "en-IN,en;q=0.9",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1",
    }

    try:
        s = requests.Session()

        # ── Step 1: GET the password form ────────────────────────────────────
        logger.info("HDFC smart statement step-1 GET: %s", url[:80])
        r1 = s.get(url, headers=headers, verify=False, timeout=30)
        r1.raise_for_status()

        ke_m  = re.search(r"name\s*=\s*['\"]ke['\"]\s[^>]*value=['\"](\w+)", r1.text, re.I)
        seq_m = re.search(r"name\s*=\s*['\"]seqence['\"]\s[^>]*value=['\"](\w+)", r1.text, re.I)
        ke_val  = ke_m.group(1)  if ke_m  else url.split("jobkey=")[1].split("&")[0]
        seq_val = seq_m.group(1) if seq_m else ""

        # ── Step 2: GET nonce token ───────────────────────────────────────────
        token_url = f"{base_url}/CRSGetToken?jobkey={ke_val}"
        rt = s.get(token_url,
                   headers={**headers, "Referer": url, "X-Requested-With": "XMLHttpRequest"},
                   verify=False, timeout=15)
        token = rt.text.strip()
        logger.debug("HDFC CRSGetToken: %s", token[:20])

        # ── Step 3: Encrypt token + password ─────────────────────────────────
        enc_pwd = _hdfc_js_encrypt(token + pdf_password)

        # ── Step 4: POST encrypted password ──────────────────────────────────
        action = f"{base_url}/webresources/app/htmlformat"
        logger.info("HDFC smart statement step-4 POST to %s", action)
        r2 = s.post(action,
                    data={"ke": ke_val, "seqence": seq_val, "pwd": enc_pwd},
                    headers={**headers,
                             "Content-Type": "application/x-www-form-urlencoded",
                             "Referer": url,
                             "Origin": re.match(r"(https?://[^/]+)", url).group(1)},
                    verify=False, timeout=60)
        r2.raise_for_status()

        # ── Step 5: Decrypt AES-ECB encrypted HTML ───────────────────────────
        data_m = re.search(r'Data\("([A-Za-z0-9+/=]+)","([A-Za-z0-9+/=]+)"\)', r2.text)
        if not data_m:
            logger.warning("HDFC smart statement: no Data() call in response (wrong password or expired link?)")
            return None

        try:
            from Crypto.Cipher import AES as _AES
        except ImportError:
            logger.error("pycryptodome not installed — cannot decrypt HDFC smart statement. "
                         "Run: pip install pycryptodome")
            return None

        aes_key    = base64.b64decode(data_m.group(1))
        ciphertext = base64.b64decode(data_m.group(2))
        cipher     = _AES.new(aes_key, _AES.MODE_ECB)
        decrypted  = cipher.decrypt(ciphertext)
        pad        = decrypted[-1]
        decrypted  = decrypted[:-pad]

        # ── Step 6: Save decrypted HTML ───────────────────────────────────────
        dest.write_bytes(decrypted)
        logger.info("HDFC smart statement saved: %s (%d bytes)", filename, len(decrypted))
        return dest

    except Exception as exc:
        logger.error("Failed to download HDFC smart statement from %s: %s", url[:80], exc)

    return None


_PENDING_FILE = RAW_DIR.parent / "hdfc_pending.json"


def _save_hdfc_pending(
    owner_id: str, url: str, subject: str, date_str: str, pdf_password: str
) -> None:
    """
    Persist HDFC smart link metadata to data/hdfc_pending.json so that
    scripts/fetch_hdfc.py can download them from a host machine with working DNS.
    Duplicate URLs (same jobkey) are silently skipped.
    """
    import json
    _PENDING_FILE.parent.mkdir(parents=True, exist_ok=True)

    existing: list[dict] = []
    if _PENDING_FILE.exists():
        try:
            existing = json.loads(_PENDING_FILE.read_text())
        except Exception:
            existing = []

    known_urls = {e["url"] for e in existing}
    if url in known_urls:
        return  # already queued

    existing.append({
        "owner_id":    owner_id,
        "url":         url,
        "subject":     subject,
        "date_str":    date_str,
        "pdf_password": pdf_password,
    })
    _PENDING_FILE.write_text(json.dumps(existing, indent=2, ensure_ascii=False))
    logger.info(
        "HDFC smart link saved to %s — run scripts/fetch_hdfc.py on your Mac to download: %s",
        _PENDING_FILE, subject,
    )


def fetch_all_accounts(
    since_days: int = 365,
    until_days: int = 0,
) -> list[FetchedAttachment]:
    """
    Iterate over all configured email accounts and download statement attachments.
    `since_days`: how far back to look (e.g. 730 = fetch emails from 730 days ago onwards).
    `until_days`: upper bound — stop at this many days ago (0 = up to today).
    Example: since_days=730, until_days=365 fetches emails between 2yrs and 1yr ago.
    Returns list of FetchedAttachment objects ready for routing.
    """
    accounts = settings.get_email_accounts()
    if not accounts:
        logger.warning("No email accounts configured. Edit config/accounts.yml or .env")
        return []

    # Run accounts concurrently — IMAP fetch is network-bound (~60-90% of sync
    # wall time) and each IMAP4_SSL connection is independent. Disable with
    # IMAP_PARALLEL=0 for sequential debugging.
    # Worker count: I/O-bound so we allow up to 2× cpu_count, hard-capped at
    # the number of accounts (no point spinning up more threads than accounts).
    import concurrent.futures as _cf
    parallel = os.getenv("IMAP_PARALLEL", "1") != "0" and len(accounts) > 1
    imap_workers = min(len(accounts), _worker_count("IMAP_WORKERS", cpu_ratio=2.0, cap=8))

    results: list[FetchedAttachment] = []
    if parallel:
        with _cf.ThreadPoolExecutor(max_workers=imap_workers) as pool:
            futures = {pool.submit(_fetch_account, acc, since_days, until_days): acc for acc in accounts}
            for fut in _cf.as_completed(futures):
                acc = futures[fut]
                try:
                    fetched = fut.result()
                    results.extend(fetched)
                    logger.info("Account %s → %d new attachments", acc["address"], len(fetched))
                except Exception as exc:
                    logger.error("Failed to fetch %s: %s", acc["address"], exc)
    else:
        for acc in accounts:
            try:
                fetched = _fetch_account(acc, since_days, until_days)
                results.extend(fetched)
                logger.info("Account %s → %d new attachments", acc["address"], len(fetched))
            except Exception as exc:
                logger.error("Failed to fetch %s: %s", acc["address"], exc)
    return results


def _parse_imap_fetch(data) -> bytes | None:
    """
    Safely extract raw RFC822 bytes from an imaplib fetch response.

    Yahoo (and some other servers) occasionally return the response as a flat
    bytes object rather than the standard list-of-tuples structure, causing
    data[0][1] to return a single byte integer instead of the full message.
    Iterate through all items and return the first real bytes payload.
    """
    if not data:
        return None
    for item in data:
        if isinstance(item, tuple) and len(item) >= 2 and isinstance(item[1], bytes):
            return item[1]
    # Fallback: concatenate any bare bytes items (some servers split the body)
    chunks = [item for item in data if isinstance(item, bytes) and len(item) > 100]
    if chunks:
        return b"".join(chunks)
    return None


def _imap_connect(acc: dict) -> imaplib.IMAP4_SSL:
    """Open an authenticated IMAP SSL connection with one retry on SSL/socket errors."""
    import time
    address  = acc["address"]
    host     = acc["imap_host"]
    port     = acc["imap_port"]
    password = acc["app_password"]
    for attempt in range(2):
        try:
            mail = imaplib.IMAP4_SSL(host, port)
            mail.login(address, password)
            return mail
        except Exception as exc:
            if attempt == 0:
                logger.warning("IMAP connect attempt 1 failed for %s (%s) — retrying in 5s", address, exc)
                time.sleep(5)
            else:
                raise


def _imap_or_search(
    mail: imaplib.IMAP4_SSL,
    date_part: str,
    domains: list[str],
    batch_size: int = 10,
) -> set[bytes] | None:
    """
    Search for emails from any of the given domains using batched IMAP OR queries.

    IMAP OR takes exactly two operands, so N domains requires a right-nested tree:
        (OR FROM "a" (OR FROM "b" (OR FROM "c" ... FROM "n")))

    We batch into groups of batch_size (default 10) to stay within server query
    length limits (some servers reject queries >8 KB).  Results are unioned.

    Returns the set of matching UID bytes, or None if the server rejects OR
    (caller falls back to individual searches).
    """
    def _build_or(doms: list[str]) -> str:
        if len(doms) == 1:
            return f'FROM "{doms[0]}"'
        if len(doms) == 2:
            return f'(OR FROM "{doms[0]}" FROM "{doms[1]}")'
        tail = f'FROM "{doms[-1]}"'
        for d in reversed(doms[:-1]):
            tail = f'(OR FROM "{d}" {tail})'
        return tail

    found: set[bytes] = set()
    for i in range(0, len(domains), batch_size):
        batch = domains[i : i + batch_size]
        or_part = _build_or(batch)
        query = f'({date_part} {or_part})'
        try:
            _, ids = mail.search(None, query)
            if ids[0]:
                found.update(ids[0].split())
        except Exception as exc:
            if "BAD" in str(exc).upper() or "NO" in str(exc).upper():
                return None   # server doesn't support OR — signal fallback
            logger.debug("IMAP OR batch search error (non-fatal): %s", exc)
    return found


def _fetch_account(acc: dict, since_days: int, until_days: int = 0) -> list[FetchedAttachment]:
    owner_id = acc["owner_id"]
    save_dir = RAW_DIR / owner_id
    save_dir.mkdir(parents=True, exist_ok=True)

    since_date = (datetime.now() - timedelta(days=since_days)).strftime("%d-%b-%Y")
    until_date = (datetime.now() - timedelta(days=until_days)).strftime("%d-%b-%Y") if until_days > 0 else None
    address    = acc["address"]

    mail = _imap_connect(acc)

    # ── Choose which IMAP folder(s) to search ────────────────────────────────
    # Financial statement emails are often auto-archived / filtered to labels,
    # so INBOX alone misses them.  Gmail's "All Mail" is the single source of
    # truth; Yahoo needs INBOX + Archive checked separately.
    imap_host = acc.get("imap_host", "")
    if "gmail" in imap_host:
        search_folders = ['"[Gmail]/All Mail"']
    elif "yahoo" in imap_host:
        # Yahoo aggressively routes broker / mutual-fund mailers to Bulk Mail.
        # Without scanning it, Coin / KFintech statements never get picked up.
        search_folders = ["INBOX", "Archive", "Bulk Mail", "Spam"]
    else:
        search_folders = ["INBOX"]

    # ── Process each folder independently (IMAP UIDs are folder-scoped) ───────
    # Track Message-ID headers to skip the same email if it appears in multiple
    # folders (e.g. Yahoo INBOX + Archive overlap after archiving).
    seen_message_ids: set[str] = set()
    results: list[FetchedAttachment] = []

    for folder in search_folders:
        try:
            status, _ = mail.select(folder)
            if status != "OK":
                logger.debug("%s → folder %s not available, skipping", address, folder)
                continue
        except Exception as exc:
            logger.debug("%s → could not select folder %s: %s", address, folder, exc)
            continue

        # Build a single batched IMAP search using nested OR criteria.
        # One roundtrip replaces the old 20-separate-query loop (~18× faster
        # for the search phase, which matters most on a 5-year import).
        # Falls back to individual searches if OR is unsupported by the server.
        candidate_ids: set[bytes] = set()
        unique_domains: list[str] = list(dict.fromkeys(
            s.split("@")[-1] if "@" in s else s
            for s in KNOWN_SENDERS.keys()
        ))
        date_part = f'SINCE "{since_date}"' + (f' BEFORE "{until_date}"' if until_date else "")
        # Yahoo's server answers a nested OR with only some of the matches (a CDSL statement that a plain FROM search finds was missing),
        # so there each domain is searched on its own.
        batched = None if "yahoo" in imap_host else _imap_or_search(mail, date_part, unique_domains)
        if batched is not None:
            candidate_ids = batched
        else:
            # Server rejected the OR query — fall back to one search per domain
            for domain in unique_domains:
                try:
                    query = f'({date_part} FROM "{domain}")'
                    _, ids = mail.search(None, query)
                    if ids[0]:
                        candidate_ids.update(ids[0].split())
                except Exception:
                    pass

        if not candidate_ids:
            logger.info("%s [%s] → 0 financial emails found", address, folder)
            continue

        logger.info("%s [%s] → %d candidate emails to process", address, folder, len(candidate_ids))

        sorted_ids = sorted(candidate_ids)
        for i, num in enumerate(sorted_ids, 1):
            # Re-establish connection on SSL EOF — Gmail drops long-idle connections
            try:
                mail.noop()
            except Exception:
                logger.warning("%s → connection lost, reconnecting (email %d/%d)", address, i, len(sorted_ids))
                try:
                    mail = _imap_connect(acc)
                    mail.select(folder)
                except Exception as exc:
                    logger.error("%s → reconnect failed: %s — aborting folder %s", address, exc, folder)
                    break

            try:
                logger.debug("Fetching email %d/%d (id=%s) from %s", i, len(sorted_ids), num, folder)
                _, data = mail.fetch(num, "(RFC822)")
                raw = _parse_imap_fetch(data)
                if raw is None:
                    logger.debug("Empty/unparseable fetch response for msg %s — skipping", num)
                    continue
                msg = email.message_from_bytes(raw)

                # Skip emails we already processed from another folder
                msg_id = msg.get("Message-ID", "")
                if msg_id and msg_id in seen_message_ids:
                    continue
                if msg_id:
                    seen_message_ids.add(msg_id)

                from_addr = msg.get("From", "")
                raw_subject = msg.get("Subject", "")
                try:
                    from email.header import decode_header
                    parts = decode_header(raw_subject)
                    subject = "".join(
                        (p.decode(enc or "utf-8") if isinstance(p, bytes) else p)
                        for p, enc in parts
                    )
                except Exception:
                    subject = raw_subject
                date_str = msg.get("Date", "")

                meta = _match_sender(from_addr)
                if meta is None:
                    continue

                body_password = _extract_pdf_password_from_body(msg)

                attachment_found = False
                for part in msg.walk():
                    # Accept explicit attachments AND inline parts carrying a financial
                    # file — Zerodha sends monthly demat PDFs as Content-Disposition: inline
                    filename = part.get_filename()
                    if not filename:
                        continue
                    ext = Path(filename).suffix.lower()
                    if ext not in ATTACHMENT_EXTENSIONS:
                        continue
                    disposition   = part.get_content_disposition() or ""
                    content_type  = part.get_content_type() or ""
                    # Skip pure body parts (no disposition, not a PDF content-type)
                    if "attachment" not in disposition and "inline" not in disposition:
                        if "pdf" not in content_type:
                            continue

                    # Skip known non-statement attachment types by filename.
                    # CDSL eCAS attachments are named like "APR2026_<BOID>_TXN.pdf" —
                    # "_TXN" means "Transaction" and is exactly what we want, so the
                    # depository senders are exempted from this filter.
                    fname_lower = filename.lower()
                    is_depository = meta["institution"] in ("CDSL", "CAMS", "KFintech")
                    skip_kws = (
                        "otp", "infographic", "cyber_security",
                        "prepayment", "emi_prepay", "newsletter", "brochure",
                        "_q1_", "_q2_", "_q3_", "_q4_",
                    )
                    if not is_depository:
                        skip_kws = ("_txn.",) + skip_kws
                    if any(kw in fname_lower for kw in skip_kws):
                        logger.debug("Skipping non-statement attachment: %s", filename)
                        continue

                    safe_name  = _safe_filename(filename)
                    dated_name = f"{date_str[:11].replace(' ', '_')}_{safe_name}" if date_str else safe_name
                    dest       = save_dir / dated_name

                    # Skip re-downloading bytes if file already on disk — historical
                    # sync re-runs become ~10× faster on the network side.
                    if not dest.exists():
                        dest.write_bytes(part.get_payload(decode=True))
                    else:
                        logger.debug("Attachment already on disk, skipping write: %s", dated_name)

                    pdf_password = _resolve_pdf_password(
                        owner_id, meta["institution"], body_password
                    )

                    results.append(FetchedAttachment(
                        owner_id=owner_id,
                        institution=meta["institution"],
                        account_type=meta["account_type"],
                        sender=from_addr,
                        subject=subject,
                        email_date=date_str,
                        file_path=dest,
                        filename=dated_name,
                        pdf_password=pdf_password,
                    ))
                    logger.debug("Saved: %s", dest)
                    attachment_found = True

                # ── Zerodha Coin allotment / redemption (HTML body only) ──────
                # Coin emails carry no attachment — the trade details are in the HTML.
                # Save the body to disk so the parser can read it like any other source.
                if not attachment_found and meta["institution"] == "Zerodha Coin":
                    html_body = ""
                    for part in msg.walk():
                        if part.get_content_type() == "text/html":
                            payload = part.get_payload(decode=True)
                            if payload:
                                html_body = payload.decode("utf-8", "replace")
                                break
                    if html_body:
                        msg_id = (msg.get("Message-ID", "") or str(num)).strip("<>")
                        safe_id = "".join(c if c.isalnum() else "_" for c in msg_id)[:40]
                        date_prefix = date_str[:11].replace(" ", "_") if date_str else "coin"
                        dest = save_dir / f"{date_prefix}_coin_{safe_id}.html"
                        if not dest.exists():
                            dest.write_text(html_body, encoding="utf-8")
                        results.append(FetchedAttachment(
                            owner_id=owner_id,
                            institution=meta["institution"],
                            account_type=meta["account_type"],
                            sender=from_addr,
                            subject=subject,
                            email_date=date_str,
                            file_path=dest,
                            filename=dest.name,
                        ))
                        attachment_found = True

                # ── CDSL demat transaction alert (HTML body only) ─────────────
                if (not attachment_found and meta["institution"] == "CDSL Alert"
                        and "transactions in your demat" in subject.lower()):
                    html_body = ""
                    for part in msg.walk():
                        if part.get_content_type() in ("text/html", "text/plain"):
                            payload = part.get_payload(decode=True)
                            if payload:
                                html_body = payload.decode("utf-8", "replace")
                                break
                    if html_body:
                        msg_id = (msg.get("Message-ID", "") or str(num)).strip("<>")
                        safe_id = "".join(c if c.isalnum() else "_" for c in msg_id)[:40]
                        date_prefix = date_str[:11].replace(" ", "_") if date_str else "cdsl"
                        dest = save_dir / f"{date_prefix}_cdslalert_{safe_id}.html"
                        if not dest.exists():
                            dest.write_text(html_body, encoding="utf-8")
                        results.append(FetchedAttachment(
                            owner_id=owner_id, institution="CDSL Alert", account_type="demat",
                            sender=from_addr, subject=subject, email_date=date_str,
                            file_path=dest, filename=dest.name,
                        ))
                        attachment_found = True

                # ── HDFC Smart Statement ──────────────────────────────────────
                if not attachment_found and meta["institution"] == "HDFC" and _is_hdfc_statement_email(subject):
                    smart_url = _extract_hdfc_smart_link(msg)
                    if smart_url:
                        smart_pwd = _derive_hdfc_smart_password(owner_id)
                        dest = _download_hdfc_smart_pdf(smart_url, owner_id, subject, date_str, save_dir, smart_pwd)
                        if dest:
                            results.append(FetchedAttachment(
                                owner_id=owner_id,
                                institution="HDFC",
                                account_type="bank",
                                sender=from_addr,
                                subject=subject,
                                email_date=date_str,
                                file_path=dest,
                                filename=dest.name,
                                pdf_password=smart_pwd,
                            ))
                        else:
                            _save_hdfc_pending(owner_id, smart_url, subject, date_str, smart_pwd)
                    else:
                        logger.warning("HDFC statement email had no smart link and no attachment: %s", subject)

            except Exception as exc:
                logger.warning("Skipping message %s in %s: %s", num, folder, exc)

    try:
        mail.logout()
    except Exception:
        pass
    return results
