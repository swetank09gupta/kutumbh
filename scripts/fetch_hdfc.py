#!/usr/bin/env python3
"""
Fetch HDFC Smart Statement HTML files from pending URLs.

Usage:
  cd /path/to/finance-os-v3
  python3 scripts/fetch_hdfc.py

What it does:
  1. Reads data/hdfc_pending.json  (written by the Docker sync process)
  2. For each pending HDFC smart statement link, performs the 4-step download:
       a. GET GetStatement.jsp  → extract ke, seqence hidden fields
       b. GET CRSGetToken       → nonce token
       c. Encrypt: token + password → _hdfc_js_encrypt()
       d. POST encrypted password → decrypt AES-ECB response → save .html
  3. Calls POST /api/sync/reprocess so Docker processes the new files
  4. Removes successfully downloaded entries from hdfc_pending.json

Requirements:
  pip3 install requests pycryptodome
"""
from __future__ import annotations

import base64
import json
import random
import re
import sys
from pathlib import Path

REPO_ROOT    = Path(__file__).resolve().parent.parent
PENDING_FILE = REPO_ROOT / "data" / "hdfc_pending.json"
API_BASE     = "http://localhost:8000"

try:
    import requests
    import urllib3
    urllib3.disable_warnings()
except ImportError:
    print("requests not found. Run:  pip3 install requests")
    sys.exit(1)

try:
    from Crypto.Cipher import AES
except ImportError:
    print("pycryptodome not found. Run:  pip3 install pycryptodome")
    sys.exit(1)


# ── encryption helper (mirrors encrypt.js on the HDFC Smart Statement page) ──

def _hdfc_js_encrypt(plaintext: str) -> str:
    """Replicate HDFC Smart Statement encrypt.js XOR cipher."""
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


# ── download one statement ─────────────────────────────────────────────────────

def _build_dest(entry: dict) -> Path:
    subject  = entry.get("subject", "")
    owner_id = entry.get("owner_id", "user")
    date_str = entry.get("date_str", "")

    acct_m = re.search(r"\*{2,3}(\d{4})", subject)
    acct4  = acct_m.group(1) if acct_m else "XXXX"

    period_m = re.search(
        r"(\d{2}-\w{3}-\d{4})\s+TO\s+(\d{2}-\w{3}-\d{4})", subject, re.IGNORECASE
    )
    if period_m:
        date_tag = period_m.group(1).replace("-", "") + "_" + period_m.group(2).replace("-", "")
    else:
        date_tag = date_str[:11].replace(" ", "_") if date_str else "unknown"

    dest_dir = REPO_ROOT / "data" / "raw" / owner_id
    dest_dir.mkdir(parents=True, exist_ok=True)
    return dest_dir / f"HDFC_{acct4}_{date_tag}.html"


def download_one(entry: dict) -> bool:
    url          = entry["url"]
    # HDFC migrated smartstatements.hdfcbank.com → smartstatements.hdfc.bank.in
    # Old emails still contain the dead domain; rewrite transparently.
    url = url.replace("smartstatements.hdfcbank.com", "smartstatements.hdfc.bank.in")
    pdf_password = entry.get("pdf_password", "")
    dest         = _build_dest(entry)

    if dest.exists():
        print(f"  Already present: {dest.name}")
        return True

    # Extract base URL from the GetStatement.jsp URL
    base_m = re.match(r"(https?://[^/]+/[^?]+/)", url)
    if not base_m:
        print(f"  Cannot parse base URL from: {url[:80]}")
        return False
    base_url = base_m.group(1).rstrip("/")

    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
        "Accept-Language": "en-IN,en;q=0.9",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1",
    }

    s = requests.Session()
    try:
        # Step 1: GET password form
        print(f"  Step 1 GET {url[:80]}...")
        r1 = s.get(url, headers=headers, verify=False, timeout=30)
        r1.raise_for_status()

        ke_m  = re.search(r"name\s*=\s*['\"]ke['\"]\s[^>]*value=['\"](\w+)", r1.text, re.I)
        seq_m = re.search(r"name\s*=\s*['\"]seqence['\"]\s[^>]*value=['\"](\w+)", r1.text, re.I)
        ke_val  = ke_m.group(1)  if ke_m  else url.split("jobkey=")[1].split("&")[0]
        seq_val = seq_m.group(1) if seq_m else ""

        # Step 2: GET nonce token
        token_url = f"{base_url}/CRSGetToken?jobkey={ke_val}"
        print(f"  Step 2 GET token...")
        rt = s.get(token_url, headers={**headers, "Referer": url, "X-Requested-With": "XMLHttpRequest"},
                   verify=False, timeout=15)
        token = rt.text.strip()

        # Step 3: Encrypt token + password
        enc_pwd = _hdfc_js_encrypt(token + pdf_password)

        # Step 4: POST encrypted password
        action = f"{base_url}/webresources/app/htmlformat"
        print(f"  Step 3 POST password (encrypted) to {action[:60]}...")
        r2 = s.post(action,
                    data={"ke": ke_val, "seqence": seq_val, "pwd": enc_pwd},
                    headers={**headers,
                             "Content-Type": "application/x-www-form-urlencoded",
                             "Referer": url,
                             "Origin": re.match(r"(https?://[^/]+)", url).group(1)},
                    verify=False, timeout=60)
        if r2.status_code == 401:
            print(f"  EXPIRED: link is no longer valid (401 Invalid Authentication) — removing from pending.")
            return "expired"
        r2.raise_for_status()

        # Step 5: Decrypt AES-ECB HTML
        data_m = re.search(r'Data\("([A-Za-z0-9+/=]+)","([A-Za-z0-9+/=]+)"\)', r2.text)
        if not data_m:
            print(f"  No Data() call in response — wrong password or expired link?")
            print(f"  Response snippet: {r2.text[:200]}")
            return False

        aes_key    = base64.b64decode(data_m.group(1))
        ciphertext = base64.b64decode(data_m.group(2))
        cipher     = AES.new(aes_key, AES.MODE_ECB)
        decrypted  = cipher.decrypt(ciphertext)
        pad        = decrypted[-1]
        decrypted  = decrypted[:-pad]

        dest.write_bytes(decrypted)
        print(f"  Saved: {dest.name} ({len(decrypted):,} bytes)")
        return True

    except Exception as exc:
        msg = str(exc)
        if "401" in msg and ("Invalid Authentication" in msg or "Unauthorized" in msg):
            print(f"  EXPIRED: link is no longer valid — removing from pending.")
            return "expired"
        print(f"  ERROR: {exc}")
        return False


def trigger_reprocess() -> None:
    try:
        r = requests.post(f"{API_BASE}/api/sync/reprocess", timeout=10)
        print(f"\nReprocess triggered: {r.json()}")
    except Exception as e:
        print(f"\nCould not trigger reprocess: {e}")
        print("Run manually: curl -X POST http://localhost:8000/api/sync/reprocess")


# ── main ─────────────────────────────────────────────────────────────────────

def main() -> None:
    if not PENDING_FILE.exists():
        print(f"No pending HDFC links found at {PENDING_FILE}")
        print("Run a sync first: curl -X POST http://localhost:8000/api/sync/")
        return

    pending: list[dict] = json.loads(PENDING_FILE.read_text())
    if not pending:
        print("hdfc_pending.json is empty — nothing to download.")
        return

    print(f"Found {len(pending)} pending HDFC statement(s) to download.\n")

    remaining = []
    downloaded = 0
    expired = 0
    for entry in pending:
        print(f"[{entry.get('owner_id')}] {entry.get('subject', '(no subject)')}")
        try:
            result = download_one(entry)
        except Exception as exc:
            print(f"  ERROR: {exc}")
            result = False

        if result is True:
            downloaded += 1
        elif result == "expired":
            expired += 1          # remove from pending — link is permanently dead
        else:
            remaining.append(entry)
        print()

    PENDING_FILE.write_text(json.dumps(remaining, indent=2, ensure_ascii=False))

    print(f"Downloaded: {downloaded}/{len(pending)}  |  Expired (removed): {expired}  |  Retryable failures: {len(remaining)}")

    if downloaded > 0:
        trigger_reprocess()
    if remaining:
        print(f"\n{len(remaining)} link(s) still failed — check the output above.")


if __name__ == "__main__":
    main()
