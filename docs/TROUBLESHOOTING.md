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
| HDFC filenames matching `FT3`, `FT4`, `FT5`, `CR*`, `IT_FINAL`, `IT_PROV`, `IT_ARHL` | Fund-transfer receipts, credit-charge receipts, and interest/tax certificates | Silently skipped (`DEBUG` log) |
| Any filename containing `TDS.CERTIFICATE` | Bank TDS / interest certificate (Axis, HDFC, SBI) | Silently skipped |
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

### Wipe and start fresh

```bash
docker-compose down -v   # removes named volumes — ALL DATA LOST
docker-compose up --build
```
