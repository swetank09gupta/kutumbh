# Parser Reference

Kutumb ingests official email statement attachments — it never scrapes bank websites. Each institution's parser lives in `app/ingestion/parsers/`.

---

## Parser map

| Institution | File | Formats handled | Trigger |
|---|---|---|---|
| HDFC Bank | `hdfc.py` | CSV (NetBanking download), PDF Layout A (Smart Statement), PDF Layout B (NetBanking Acct_Statement), HTML (current Smart Statement — AES-decrypted) | Email attachment from HDFC |
| SBI | `sbi.py` | CSV | Email attachment from SBI |
| Axis Bank | `axis.py` | CSV | Email attachment from Axis |
| Union Bank of India | `union_bank.py` | PDF (password-protected) | Email attachment from Union Bank |
| Zerodha Kite | `zerodha.py` | CSV (holdings), CSV (tradebook), CSV (contract notes) | Email attachment from Zerodha |
| Zerodha Coin | `zerodha_coin.py` | PDF — old format (2021–mid-2024) and new format (mid-2024+) | Email attachment from Zerodha Coin |
| CAMS / KFintech CAS | `cas_mf.py` | PDF (CAMS consolidated account statement) | Email from CAMS / KFintech |
| KFintech folio | `kfintech_folio.py` | PDF | Email from KFintech |
| CDSL CAS | `cdsl_cas.py` | PDF | Email from CDSL |

---

## HDFC Bank

**File:** `app/ingestion/parsers/hdfc.py`

HDFC is the most complex parser — three distinct formats from the same institution:

### Format 1: CSV (NetBanking download)
Standard 7-column CSV. Detected by `.csv` extension. Most reliable — use this if you have a choice.

### Format 2: HTML Smart Statement (current default)
HDFC's current email statement is an HTML file inside a ZIP, encrypted with AES-ECB using the PDF password as key. The parser:
1. Detects the `.zip` attachment
2. Decrypts using the derived password (`name[:4].upper() + DDMM`)
3. Parses the HTML table with `pd.read_html`

### Format 3: PDF — Layout A
Older Smart Statement PDFs. One transaction per row, 7 columns. Extracted with `pdfplumber`.

### Format 4: PDF — Layout B
NetBanking "Account Statement" PDFs. All transactions on a page share one row with embedded newlines. The parser detects Layout B by presence of `\n` in data cells and uses a balance-delta approach (closing balance − opening balance = transaction amount) rather than reading a debit/credit column.

**Layout B known quirk:** Headerless continuation pages (page 2+) have no column header row. The parser tracks `prev_balance` across pages to handle this.

If your HDFC PDF uses a CIF-based password (different from `name[:4]+DDMM`), set `hdfc_pdf_password` in `accounts.yml`.

---

## SBI

**File:** `app/ingestion/parsers/sbi.py`

SBI sends a password-protected CSV. Password formula: `name[:4].upper() + DDMMYYYY`.

SBI e-MOD auto-sweep transactions (internal FD creation from excess balance) are categorised as `investment/fd_rd`. INB MBS transfers (internet banking) are categorised as `transfer` unless labelled PPF.

---

## Axis Bank

**File:** `app/ingestion/parsers/axis.py`

Standard CSV. No password. Axis narration format differs from HDFC/SBI — the categoriser's generic rules cover most Axis transactions.

---

## Union Bank of India

**File:** `app/ingestion/parsers/union_bank.py`

Password-protected PDF. Password formula: `name[:4].upper() + DDMM`.

**Known issue:** Union Bank occasionally changes their PDF password scheme without notice. If parsing fails with a decryption error, check the actual password used on a recent statement and set `hdfc_pdf_password` (reused field) in `accounts.yml`, or open the PDF manually and inspect the format — see [TROUBLESHOOTING.md](TROUBLESHOOTING.md).

---

## Zerodha Kite

**File:** `app/ingestion/parsers/zerodha.py`

Three CSV types detected by filename / column headers:
- **Holdings** — current portfolio snapshot
- **Tradebook** — buy/sell history
- **Contract notes** — daily settlement PDFs (used for exact trade prices)

ISIN values in the holdings CSV are used to join Coin mutual fund transactions via the AMFI NAV file.

**Contract-note PDFs** come in three layouts, all feeding the instrument ledger (quantity, price, trade no, printed settlement date):
- table layout (mid-2024+): per-ISIN buy/sell columns
- text-line layout (2022 → mid-2024): `order# time trade# time SYMBOL - EQ / B NSE qty rate net (total)` with the ISIN on the next line
- text-line layout with `SYMBOL-EQ/ISIN` in one cell

**Monthly demat statement** (`…-sot.pdf`, `transaction-with-holding-statement`): the true date is "Holdings as on …". Movement lines are recorded as the change in the Balance column (Zerodha's "Early Pay-in" lines list a debit and its reversal while the balance does not move). The 2021 layout (`HOLDING AS ON dd/mm/yyyy`, collapsed columns) has its own deterministic parser — the LLM fallback once read the ₹ value column as units. Closing balances are stored per statement family in `statement_balances` (Zerodha / CDSL), never merged: CDSL also lists holdings outside this demat.

---

## Zerodha Coin

**File:** `app/ingestion/parsers/zerodha_coin.py`

Coin changed its email format in mid-2024. Both are supported:

### Old format (2021–mid-2024)
Tabular PDF — one row per allotment/redemption with scheme name, units, NAV, amount. Detected by presence of structured table rows.

### New format (mid-2024+)
Block-style PDF — per-fund sections with Folio number, NAV, ₹ amount, and unit count on separate lines. The parser matches fund names to AMFI ISINs via `NAVAll.txt` to enable XIRR calculation.

If old-format rows are detected they take priority (they're more reliable). The new-format parser falls back gracefully if blocks don't parse.

Also handled: **"Purchase confirmation"** (Coin v3, 2025+; same block layout as the new format) and **"mutual fund redemption has been processed"** (a `Fund Settl. type Units Nav Amount` table — the one that carries units, NAV and amount for a sale). "Your sell order was processed" is deliberately skipped (it restates the redemption mail). "Order placed" / SIP-placed / IPO-not-allotted / AMC notices carry no trade and are ignored. The filename has no year ("Tue,_06_May_…"); the year is the latest one in which that day falls on that weekday.

---

## Bank alerts (HDFC, SBI)

**File:** `app/ingestion/parsers/bank_alert.py` — body-only mails from `hdfcbank.bank.in` / `hdfcbank.net` / `alerts.sbi.bank.in`, parsed straight from IMAP into `bank_alerts` (nothing saved under `data/raw`). Evidence only; see ADR-0025. Supported templates: HDFC UPI debit ("You have done a UPI txn") and credit, ACH/UMRN debit, "New Deposit Alert"; SBI "Debited/Credited INR … -<narration>. Avl Bal", NACH debit, "has credit/debit for UPI/…", NEFT originated/received (UTR), standing-instruction "Transaction success". Anything else (TDS, interest certificates, marketing) is ignored.

## HDFC home-loan prepayment letters

**File:** `app/ingestion/parsers/loan_letter.py` (fetched by `loan_fetcher.py`) — PDF attached to "Prepayment Letter - HDFC Bank Ltd Home Loan" mails. Only the *receipt* wording ("We are in receipt of Rs. N towards Part prepayment") is an event; the enquiry wording is a quote and is skipped. Gives loan number, date, amount prepaid, principal outstanding afterwards, revised EMI, rate and EMIs left.

## CDSL transaction alerts

**File:** `app/ingestion/parsers/cdsl_alert.py` — body-only mails from `services@cdslindia.co.in`, subject "Transactions In Your Demat Account": one row per ISIN with units, Credit/Debit and timestamp, plus the demat suffix (`*08489463`). Quantity only (no price, no amount), so they are stored as demat movements for the ledger and never become cashflows. Saved as `<date>_cdslalert_<id>.html`.

---

## CAMS / KFintech CAS

**File:** `app/ingestion/parsers/cas_mf.py`, `app/ingestion/parsers/kfintech_folio.py`

Uses the [`casparser`](https://github.com/codereverser/casparser) library (installed with `--no-deps` in the Dockerfile to avoid pdfminer version conflict with pdfplumber). Password: PAN uppercase.

CAS PDFs contain the full transaction history for all mutual funds held through that RTA. A single CAS import covers all CAMS-serviced or KFin-serviced funds.

---

## CDSL CAS

**File:** `app/ingestion/parsers/cdsl_cas.py`

CDSL sends a consolidated demat statement PDF. Password: PAN uppercase. Covers equity holdings and SGB units held in demat form.

---

## Adding a new parser

1. Create `app/ingestion/parsers/<institution>.py` subclassing `BaseParser` from `base.py`
2. Implement `parse(self, path: Path) -> ParseResult`
3. Register the parser in `app/ingestion/email_fetcher.py` in the `_get_parser()` dispatch
4. Add `salary_keywords` / categoriser rules for the institution's narration format if needed

`ParseResult` is a dataclass with `transactions: list[dict]` and `account_id: str`. Each transaction dict must include `date`, `amount`, `description`, `balance` (optional), `reference` (optional).

---

## Debugging a failed parse

1. **Check logs:** `docker logs finance_app 2>&1 | grep -i "parse\|error\|skip"`
2. **Run parser directly:**
   ```bash
   docker exec -it finance_app python3 -c "
   from app.ingestion.parsers.hdfc import HdfcParser
   from pathlib import Path
   r = HdfcParser().parse(Path('/path/to/statement.pdf'))
   print(len(r.transactions), 'transactions')
   print(r.transactions[:2])
   "
   ```
3. **Ollama fallback:** If the deterministic parser returns 0 transactions, the system automatically retries via Ollama (`llama3.2`). Check `docker logs finance_ollama` if the fallback also fails.
4. **PDF password:** Open the PDF manually in a viewer to confirm the expected password is correct. See [TROUBLESHOOTING.md](TROUBLESHOOTING.md) for institution-specific password formulas.
