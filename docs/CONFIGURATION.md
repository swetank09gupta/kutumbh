# Configuration Reference

Kutumb is configured through two files, both git-ignored so they never enter version control:

| File | Purpose |
|---|---|
| `.env` | Secrets — database password, Grafana password, email app passwords |
| `config/accounts.yml` | Household profile — owners, email accounts, goals, salary patterns |

Copy the examples on first setup:

```bash
cp .env.example .env
cp config/accounts.example.yml config/accounts.yml
```

---

## `.env` reference

| Variable | Required | Default | Description |
|---|---|---|---|
| `POSTGRES_USER` | yes | `financeuser` | PostgreSQL username |
| `POSTGRES_PASSWORD` | yes | — | PostgreSQL password — use a strong random string |
| `POSTGRES_DB` | yes | `financedb` | Database name |
| `GRAFANA_USER` | yes | `admin` | Grafana admin username |
| `GRAFANA_PASSWORD` | yes | — | Grafana admin password |
| `USER_GMAIL` | one email required | — | Gmail address for primary owner |
| `USER_GMAIL_APP_PASSWORD` | one email required | — | Gmail app password (not your login password) |
| `USER_YAHOO` | optional | — | Yahoo address for primary owner |
| `USER_YAHOO_APP_PASSWORD` | optional | — | Yahoo app password |
| `WIFE_GMAIL` | optional | — | Gmail for second owner |
| `WIFE_GMAIL_APP_PASSWORD` | optional | — | Gmail app password for second owner |
| `WIFE_YAHOO` | optional | — | Yahoo for second owner |
| `WIFE_YAHOO_APP_PASSWORD` | optional | — | Yahoo app password for second owner |
| `EMAIL_CHECK_INTERVAL_HOURS` | no | `6` | How often to poll email (hours) |
| `OLLAMA_PARSE_MODEL` | no | `llama3.2` | Ollama model for PDF fallback parsing |
| `ACCEPTED_GAPS_FILE` | no | `config/accepted_gaps.json` | Statement months you have confirmed the bank never issued (see below) |

**Getting app passwords (not your login password):**
- Gmail: myaccount.google.com → Security → 2-Step Verification → App passwords
- Yahoo: security.yahoo.com → Generate app password → Mail

---

## `config/accepted_gaps.json` (private, git-ignored, optional)

A statement month the bank never issued cannot be filled and nothing is invented; the balance today is the newest statement or alert. List it here and the balance-chain check stops warning about it (ADR-0040). Only an exact owner + account + month counts:

```json
[{"owner": "user", "account": "SBI 8660", "month": "2024-10", "note": "No Oct 2024 statement exists at the bank"}]
```

`account` is as the check prints it (`/api/health/continuity`: institution and account ref).

---

## `config/accounts.yml` reference

### `email_accounts`

List every email inbox that receives bank/broker statements.

```yaml
email_accounts:
  - owner_id:    "user"          # must match an id in owners:
    owner_name:  "Self"          # display label only
    address:     "${USER_GMAIL}" # references .env variable
    app_password: "${USER_GMAIL_APP_PASSWORD}"
    imap_host:   "imap.gmail.com"
    imap_port:   993
```

| Field | Description |
|---|---|
| `owner_id` | Must match an `id` in the `owners:` section |
| `owner_name` | Display label shown in dashboards |
| `address` | Email address — use `${ENV_VAR}` to reference `.env` |
| `app_password` | App password — use `${ENV_VAR}` to reference `.env` |
| `imap_host` | IMAP server hostname |
| `imap_port` | IMAP port — almost always `993` (TLS) |

---

### `owners`

One entry per person in the household. Used to derive PDF passwords automatically at runtime, compute 80C limits, and drive goal projections.

```yaml
owners:
  - id:     "user"
    name:   "Your Name"
    dob:    "1990-03-15"     # YYYY-MM-DD
    pan:    "ABCDE1234F"     # uppercase
    mobile: "9876543210"     # 10-digit
    tax_regime:          "old"
    epf_monthly:         1500
    home_loan_share_pct: 50
    salary_keywords:
      - "acmecorp"
    salary_patterns: []
```

| Field | Required | Description |
|---|---|---|
| `id` | yes | Short identifier used in API calls (`owner_id=user`) |
| `name` | yes | Full name — used to derive PDF passwords |
| `dob` | yes | Date of birth `YYYY-MM-DD` — used to derive PDF passwords |
| `pan` | yes | PAN number uppercase — used to open Zerodha / CAMS / KFin PDFs |
| `mobile` | no | 10-digit mobile — last 4 digits used as SBI fallback password |
| `tax_regime` | no | `"old"` or `"new"`. Old regime enables 80C dead-weight ELSS check. Default `"old"` |
| `epf_monthly` | no | Employee EPF contribution per month (₹). Counted toward 80C. Default `0` |
| `home_loan_share_pct` | no | Your share of the household home loan (0–100). Loan principal × this% counts toward 80C. Default `0` |
| `salary_keywords` | no | Plain-text employer name fragments (see below) |
| `salary_patterns` | no | Full Python regex patterns for salary matching (advanced) |

#### PDF password derivation

Kutumb computes PDF passwords from `name` + `dob` — you never store them:

| Institution | Formula | Example (name=Ravi Kumar, dob=1990-03-15) |
|---|---|---|
| HDFC | `name[:4].upper()` + `DDMM` | `RAVI1503` |
| SBI | `name[:4].upper()` + `DDMMYYYY` | `RAVI15031990` |
| Axis | `name[:4].upper()` + `DDMMYYYY` | `RAVI15031990` |
| Union Bank | `name[:4].upper()` + `DDMM` | `RAVI1503` |
| Zerodha / CAMS / KFin / CDSL | PAN uppercase | `ABCDE1234F` |

If your HDFC PDF uses a NetBanking CIF-based password (different formula), set it explicitly:

```yaml
hdfc_pdf_password: "268955063"   # overrides the dob-derived password
```

Other passwords the bank mails state, tried in addition to the formulas (Union Bank 2020-21 statements, HDFC home-loan tax certificates):

```yaml
union_bank_account_no: "123456789012345"
hdfc_loan_account_no: "12345678"
```

#### Salary keyword matching

The categoriser already catches narrations containing `salary`, `sal credit`, `payroll`, etc. You only need `salary_keywords` when your bank's narration is just the employer name with no "salary" word, e.g. `NEFT FROM ACMECORP`.

```yaml
salary_keywords:
  - "acmecorp"          # matches "NEFT FROM ACMECORP", "ACMECORP SAL", etc.
  - "acme technologies"
```

Keywords are case-insensitive and matched as substrings. No regex knowledge needed.

For complex narration patterns (e.g. `NEFT.*ACME|ACME.*NEFT`), use `salary_patterns` instead — these are raw Python `re` expressions.

---

### `children`

Optional. Used to compute age-based target dates for `child_education` and `child_marriage` goals. If absent, goals use their stored `target_date`.

```yaml
children:
  - id:            "child1"
    name:          "Child Name"
    dob:           "2020-04-15"
    education_age: 18      # higher education starts at this age
    marriage_age:  25      # marriage planning at this age
```

---

### `goals`

Seeded into the database on first startup. Amounts are in **today's money** — inflation adjustment is applied automatically by the projection engine.

```yaml
goals:
  - owner_id:             "family"
    name:                 "Child Higher Education"
    goal_type:            "child_education"
    target_amount:        5000000     # ₹50 L in today's value
    target_date:          "2040-06-01"
    expected_return_rate: 12.0        # % annual returns
    notes:                "Engineering / MBA fund"
```

| Field | Description |
|---|---|
| `owner_id` | `user`, `wife`, or `family` |
| `goal_type` | `child_education`, `child_marriage`, `retirement`, `emergency`, `health`, `custom` |
| `target_amount` | Corpus needed in today's rupees |
| `target_date` | Overridden by age-based date for `child_*` and `retirement` goals when `children` / `dob` are set |
| `expected_return_rate` | Assumed annual return on investments (%) |
| *(inflation)* | Not a goal field any more. One app-wide rate, automatic: the 10-year average of India CPI (World Bank). Override or clear it on the Goals page or with `PUT/DELETE /api/goals/settings/inflation` (ADR-0019). A leftover `inflation_rate` key in a goal is ignored. |

Goals can be created / edited after startup via the API:

```bash
curl -X POST http://localhost:8000/api/goals/ \
  -H "Content-Type: application/json" \
  -d '{"owner_id":"user","name":"Home Renovation","goal_type":"custom","target_amount":800000,"target_date":"2027-03-31","expected_return_rate":8.0}'
```

---

## After changing configuration

| Change | Action needed |
|---|---|
| Added / changed email account | Restart: `docker-compose restart app` |
| Changed owner fields (dob, pan, etc.) | Restart: `docker-compose restart app` (PDF passwords recomputed at runtime) |
| Changed tax / EPF / loan fields | These seed the DB on first run; update live via `PUT /api/profile/{owner_id}` |
| Added salary keywords | Restart + trigger re-categorisation: `POST /api/sync/recategorize` |
| Changed goals | Restart to reseed, or use `POST /api/goals/` to add without restarting |
