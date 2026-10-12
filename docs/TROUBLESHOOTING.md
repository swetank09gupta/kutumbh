# Troubleshooting

Common problems and fixes. Check `docker logs finance_app` first — most errors are logged with context.

---

## Startup

### `docker-compose up` fails — port already in use

```
Error: port 5432 is already allocated
```

A local PostgreSQL is running on the host. Either stop it (`brew services stop postgresql`) or change the host-side port in `docker-compose.yml`:

```yaml
ports:
  - "5433:5432"   # host:container
```

Update `DATABASE_URL` in `.env` accordingly.

---

### App container keeps restarting

```bash
docker logs finance_app --tail 50
```

Most common causes:
- **Bad `.env`** — missing `POSTGRES_PASSWORD` or malformed value
- **DB not ready** — Postgres is still initialising; the app has a retry loop but on slow machines it can time out. Run `docker-compose restart app` once Postgres is healthy.
- **Import error** — a Python dependency missing from the image. Run `docker-compose build --no-cache` to rebuild.

---

### Grafana shows "No data"

1. Confirm the app has completed at least one sync: `curl http://localhost:8000/api/accounts/networth?owner_id=family`
2. Check Grafana datasource: Settings → Data Sources → PostgreSQL → Test
3. If the test fails, confirm `POSTGRES_PASSWORD` in `.env` matches `GF_DATABASE_PASSWORD` — they share the same variable via `docker-compose.yml`.

---

## Email / IMAP

### No statements being fetched

```bash
curl -X POST http://localhost:8000/api/sync/now
docker logs finance_app --tail 30
```

Check for:
- **Wrong app password** — use the app password, not your Gmail/Yahoo login. Regenerate if unsure.
- **Gmail "Less secure app access"** — not needed with app passwords. Ensure 2-Step Verification is on, then generate an app password.
- **Yahoo IMAP disabled** — go to Yahoo Mail → Settings → More Settings → Mailboxes → enable IMAP.
- **Placeholder not replaced** — accounts with `address: your_email@gmail.com` or `xxxx` app password are silently skipped.

---

### Emails found but attachments not parsed

```
INFO: skipping attachment — no parser matched
```

The attachment filename doesn't match any parser's detection pattern. Common causes:
- Bank changed the filename format (e.g. HDFC renamed from `Statement.pdf` to `AccountStatement.pdf`)
- The attachment is a forward/reply with a different MIME structure

Fix: open the raw email, save the attachment, and run the parser directly (see [PARSERS.md — Debugging](PARSERS.md#debugging-a-failed-parse)).

---

## Numbers disagree between pages
Open `/api/health/consistency` (or the banner at the top of the app). Every check that fails names the two figures that differ. The usual fixes: run a sync (it re-runs cleanup, the ledger, snapshots and goals in that order), or label the payees in the Transactions → "Label payees" card. See ADR-0024 for what each number means.

## An expense or income looks wrong
Expense excludes transfers, investments and loan prepayments; income is salary, interest/dividends and refunds; other credits (deposits, gifts) are not income until tagged. A payment to a person shows as "other" until you label that payee once — it is then applied to every past and future payment. Own-account transfers are found by matching amount and date across accounts, or by your name in the narration.

## Holdings / reconciliation

### Holdings differ from Zerodha Console
`POST /api/ledger/verify` with the quantities you see; then `GET /api/ledger/issues?severity=error`. Usual causes, in order: a Coin/contract-note e-mail never reached the mailbox (`missing_trade`; check `WIFE_GMAIL*`-style credentials), a statement month is missing (`missing_statement`), or a trade settled after the last statement (`pending_trade`, resolves by itself).

### A mailbox gets no Coin mails
Not every account receives Coin allotment mails (the wife's Yahoo does not). The CDSL "Transactions In Your Demat Account" alerts still arrive and the ledger uses them for quantity (`alert_without_trade`); cost stays partial until a tradebook CSV (Console → Reports → Tradebook, Mutual funds) fills the trades — the ledger de-duplicates it against any Coin mails.

### Cost shows "partial"
Some units have no traced purchase (bought before the earliest contract note/Coin e-mail, SGB allotments, transfers). Invested/P&L are left empty rather than guessed. Supplying a tradebook CSV for that period or the Console holdings export fixes it.

### "ledger_exceeds_statement" / "trades_exceed_statement"
Trades add up to more than the statement balance moved: a duplicate, a wrong date, or a trade filed under a different ISIN. `POST /api/ledger/dedupe` lists cross-source duplicates (dry run).

## PDF parsing

### HDFC PDF — wrong password / decryption error

HDFC uses two password schemes:
- **Smart Statement (email):** `name[:4].upper() + DDMM` — e.g. `RAVI1503` for Ravi Kumar born 15 Mar
- **NetBanking download:** CIF-based numeric password (different from the above)

If your HDFC PDFs come from NetBanking download rather than email, set the explicit password in `accounts.yml`:

```yaml
owners:
  - id: "user"
    hdfc_pdf_password: "268955063"   # your CIF-based password
```

---

### Union Bank PDF — failed to parse / bad password

Union Bank occasionally changes their PDF encryption without announcement. Steps:
1. Open the PDF manually — try `name[:4].upper() + DDMM` first
2. If that fails, check for a password hint in the email body
3. Set the working password explicitly in `accounts.yml`:
   ```yaml
   owners:
     - id: "user"
       hdfc_pdf_password: "RAVI1503"   # reuse this field; it overrides the derived password
   ```
4. Trigger a re-parse: `curl -X POST http://localhost:8000/api/sync/now`

---

### CAMS / KFin CAS PDF — `casparser` import error

`casparser` is installed with `--no-deps` in the Dockerfile to avoid a `pdfminer.six` version conflict with `pdfplumber`. If you're running outside Docker:

```bash
pip install casparser --no-deps
pip install charset-normalizer click  # casparser's minimal deps
```

---

### Zerodha Coin — 0 transactions parsed

Coin changed its PDF format in mid-2024. If you have older statements:
- The old-format parser handles 2021–mid-2024 tabular PDFs
- The new-format parser handles mid-2024+ block-style PDFs

If neither matches, check the Coin PDF visually — if it looks like a new layout, open a GitHub issue with a redacted sample (remove scheme names / amounts).

---

### High `parse_errors` count despite normal-looking sync

Some document types that arrive as PDF email attachments are **not** transaction statements and are intentionally skipped without counting as errors. If you see more errors than expected after a fresh sync, check first whether the emails in question match these known non-statement categories:

| Pattern | What it is | Behaviour |
|---------|-----------|-----------|
| HDFC filenames matching `FT3`, `FT4`, `FT5`, `CR*`, `IT_ARHL` | Fund-transfer receipts, credit-charge receipts, interest certificates | Silently skipped (`DEBUG` log) |
| HDFC `IT_FINAL…` / `IT_PROV…` | Home-loan tax certificates (lender's principal/interest per FY; password = loan account number) | Not statements: read by the loan-certificate step of the update (ADR-0035), not by the router |
| Any filename containing `TDS.CERTIFICATE` | Bank TDS / interest certificate (Axis, HDFC, SBI) | Silently skipped |
| "on N% of the value — ₹X has no purchase history yet" | Units in the demat with no trade, no unit line and no matching bank debit | Import the Console tradebook CSV for that account (Reports → Tradebook, Mutual funds); derived trades are replaced automatically |
| A deposit shows as closed but is not | The newest SBI statement's FD table did not list it (parse failure?) | Check `deposit_snapshots` for that statement date; re-read it with `python -m scripts.backfill_deposit_history` |
| Deposit value or accrued interest looks off after a parser change | Parse results are cached | Bump `_CACHE_SCHEMA` in `parsers/base.py`, then `python -m scripts.backfill_fd_accrued` (or reprocess) |
| Deposit row shows "x% FD rate" instead of a measured XIRR | Fewer than ~80% of its monthly "INT TRF FRM" credits are in the bank statements, or the principal changed | Import the missing statements; the measured return appears on the next sync |
| `ppf_years_reconcile` warns | A PPF year does not close: a deposit whose month cannot be pinned down, or a balance lower than deposits + interest explain | `/api/health/ppf` shows each year; add the missing statement or deposit |
| PPF rate looks stale | The NSI page was unreachable; last known table is used | Check `data/cache/ppf_rates.json`; it refreshes weekly |
| Return shows plain gain, not a rate | Money held under a year on average | Intended (SEBI/AMFI convention) |
| `alerts_have_statement_rows` warns | A bank alert falls inside a statement's period but has no statement row | Open `/api/health/alerts` → `unmatched`; the statement parser dropped that line, or the statement page is missing |
| Alert never shows up | Its wording is not one of the templates in `PARSERS.md` | Add a pattern to `bank_alert.py` with a test |
| Union Bank PDF < 50 KB with all passwords failing | Cheque-clearance / debit-alert / KYC notification letter | Skipped with `INFO` log |

These are filtered in `app/ingestion/attachment_router.py` before the parser is invoked, so they do not appear in the `parse_errors` counter in the sync response. If you see them in `docker logs finance_app` at `DEBUG` level, that is expected.

If you believe a file is incorrectly skipped (e.g. HDFC renames a genuine statement to match a skip pattern), adjust the regex in `attachment_router.py → route_and_parse()`.

---

### Ollama fallback not working

If the deterministic parser fails and Ollama is the fallback:

```bash
# Check Ollama is running
docker logs finance_ollama --tail 20

# Check llama3.2 is pulled
docker exec finance_ollama ollama list

# Pull if missing
docker exec finance_ollama ollama pull llama3.2
```

Ollama needs ~2 GB RAM for `llama3.2` (3B). On a machine with <3 GB free, use the smaller model:

```bash
# .env
OLLAMA_PARSE_MODEL=llama3.2:1b
```

---

## Data / categorisation

### Transactions are being double-counted

Kutumb uses fingerprint-based dedup — reprocessing the same statement should never create duplicates. If you see doubles:

```bash
# Check for duplicate fingerprints
docker exec finance_app python3 -m scripts.run_maintenance dedup
```

If duplicates persist, they likely have different fingerprints due to encoding differences (e.g. different whitespace in narration from two sources). File an issue with the duplicate transaction details (redacted).

---

### Holdings (portfolio/goals page) shows error or inflated values after DB reset

**Root cause:** The `latest_holdings_deduped` PostgreSQL view (used by the portfolio API, goals engine, and recommendations) is not a SQLAlchemy ORM model — it is created by `_migrate_db()` at startup. If you wiped and recreated the DB before this fix was added, the view does not exist and those endpoints fail with `relation "latest_holdings_deduped" does not exist`.

**Fix:** The view is now created idempotently in `_migrate_db()` on every startup (`CREATE OR REPLACE VIEW`). Restart the app container to apply:

```bash
docker-compose restart app
```

The net-worth endpoint is unaffected (it uses a separate Python-side dedup), which is why Grafana net-worth charts could still render while the holdings page was broken.

---

### Salary credits not categorised as income/salary

1. Check if the bank narration contains "salary", "sal credit", or "payroll" — these are caught by the generic rule automatically.
2. If not, find the exact narration text in your statement, then add it to `accounts.yml`:
   ```yaml
   owners:
     - id: "user"
       salary_keywords:
         - "acmecorp"   # plain substring match, case-insensitive
   ```
3. Restart and re-run categorisation:
   ```bash
   docker-compose restart app
   curl -X POST http://localhost:8000/api/sync/recategorize
   ```

---

### XIRR returns `None` or `NaN`

XIRR requires at least one inflow and one outflow with different dates. Common causes:
- Coin transactions without a matched ISIN — run `docker exec finance_app python3 -m scripts.backfill_coin_isins`
- SGB purchase with no matching redemption yet — expected; XIRR will compute once a redemption or maturity event is recorded
- Single transaction in a fund — XIRR needs at minimum a purchase + current NAV valuation

---

### Market signals show stale / missing data

NSE P/E data and USD/INR are cached for 12 hours. If signals are stale:

```bash
# Force refresh
curl -X POST http://localhost:8000/api/goals/market-signals/refresh
```

If the fetch fails entirely (NSE endpoint changed), recommendations fall back to ignoring P/E zones and note "market data unavailable" in the output.

---

## Backups & data recovery

### Export all data before migrating machines

```bash
docker exec finance_postgres pg_dump -U financeuser financedb > kutumb_backup.sql
```

### Restore on a new machine

```bash
docker-compose up -d postgres   # start only postgres
docker exec -i finance_postgres psql -U financeuser financedb < kutumb_backup.sql
docker-compose up -d            # start the rest
```

### "automatic update is running" warning
The last scheduled update is older than 3 intervals, or failed (the warning says which). `docker logs finance_app | grep -i "scheduled sync"` shows why (mailbox login, IMAP timeout). One failing source (alerts, loan letters, the RBI page) is logged and skipped; the others still run. Pressing Sync runs the same pipeline.

### "loan reconciles with lender" warning
`/api/loan` shows which: the newest HDFC letter differs from the roll-forward (a letter arrived that the EMIs since the last one do not explain, or an EMI month is missing from the data), the EMIs left disagree with the schedule, or an EMI debit due more than 4 days ago was not seen (check the HDFC alert / statement). Older intervals that cannot be reproduced stay listed there and do not warn.

### "sgb coupons reconcile" warning
`/api/sgb` says which: coupons due since a date were not found (credited to an account whose statements we do not read, or the bank narration changed), a held bond has no matching coupon, or coupons arrive for a bond no statement lists. For the last, add the bond in Manual Assets (type SGB, name containing the series, grams) and the warning clears. A new bank narration is fine: matching is by amount and date, the text only narrows the search.

### "dividends reconcile" warning
`/api/tax/dividends` lists the credits that match no stock and ex-date: usually shares bought before the trade history (their count at the ex-date is unknown) or a stock whose ex-date history Yahoo no longer serves (renamed or delisted). They stay counted as dividends at slab in the tax view, marked "not matched to a stock".

### The tax figures look off
Gains use gross trade prices (brokerage and stamp duty are not netted, STT is not included) and no corporate actions: a split or bonus makes FIFO lots wrong for that stock. Sales of units bought before the trade history are listed and left out. Deposit interest counts what was credited plus TDS notices; interest that accrues in a cumulative deposit without being credited is not seen (the card says so when the bank is already deducting TDS).

### A PDF will not open ("all passwords failed")
`docker exec finance_app python -c "…"` is not needed: the router tries every owner's derived password for that bank. A file that still fails uses a rule we do not know (the 2020-21 Union Bank statements, payroll tax sheets). Tell the app the password by adding it as an extra candidate in `_all_pdf_passwords`, never in the repo; then delete nothing, the next sync reads the file.

### A sale shows "cost unknown" in the tax view
First check the raw mail: a Coin mail that prints the same line twice is two orders (ADR-0034); a contract note that parses to no trades is a new layout (docs/PARSERS.md). Units from an IPO allotment, a bonus issue or a transfer-in have no contract note; they stay listed rather than costed.

### A trade or holding appears twice after a rescan
The scheduler re-reads everything since the last successful run (plus 3 days; 7–400 days),  so storage must be idempotent. Coin orders are identified by owner, folio, date, side, units and amount (not ISIN); the maintenance step `dedupe_coin_trades` removes older copies. A new duplicate means another identity that includes something a later fix can change: add its test to `tests/test_coin_duplicates_db.py`.

### Wipe and start fresh

```bash
docker-compose down -v   # removes named volumes — ALL DATA LOST
docker-compose up --build
```
