# Handover: where things stand (updated 2026-10-24)

Read this first, then `CLAUDE.md` (working agreement), `docs/INVARIANTS.md`, `docs/DECISIONS.md`. Latest decisions: ADR-0043 (one calculator), 0044 (matching), 0045 (personal facts), 0046 (audit), 0047 (retirement + senior health), 0048 (SBI renewals), 0049 (UI audit). Private config `config/accounts.yml` and `.env` are git-ignored: never print them.

## What the app is
Family finance on FastAPI + SQLAlchemy + Postgres (docker compose), one-page UI `app/static/index.html` (Overview, Sync, Transactions, Investments, Goals, Accounts, Manual Assets; Grafana is embedded only for the net-worth history on Overview), APScheduler jobs (statements + bank alerts every ~6 h, daily sweep). Every page reads shared definitions: `v_cashflow`, `latest_holdings_deduped`, `build_positions`, `project_all_goals`. One definition per number; no page recomputes.

## Goals: how a card is made
- `engine/goals.project_all_goals` is the only calculator. `planning/goal_plan.overlay` fills target, SIP for 80%, chance, return, inflation from market data (`app/planning/`, sourced params in `params.yml`). Goals: Emergency, Child Higher Education, **Retirement & Senior Health** (one goal; senior health is a stream inside it), Child Marriage.
- Corpus (`refresh_corpus_from_networth`): emergency = bank + every live FD/RD; others = everything tagged (`goal_plan.holdings_by_owner`: holdings + manual assets). Tags are manual (set in the UI, never moved) or auto (`planning/matching.py`, after each sync and in the daily sweep). Deposits are always the emergency fund. There is no fixed fund-to-goal list: matching uses measured fund/stock profiles and each goal's risk budget (`docs/PLANNING.md` §6).
- Funding order: `goal_plan.apply_funding` (emergency, then by date; keeps a working SIP). The app only suggests SIPs; the mandates are the owner's.
- Personal facts in config: EPF (both), NPS (wife, contributions only), pay, family history, term plan (return of premium, cancer rider), loan shares (33 / 34 / father 33, not an owner), `goal_priority`.

## Money in, money out: what to know about the data
- SBI's alert "available balance" includes the owner's linked deposits: bank balance = print − the owner's live SBI deposits (ADR-0046). SBI mails nothing for UPI payments or mandate debits (those rows appear when the statement does); Zerodha "Payment success" mails fill in transfers to the broker. SBI deposits auto-renew: a matured deposit with no payout credit stays live, maturity one year on, until the statement shows the term (ADR-0048).
- The wife's mailbox is Yahoo: searched one sender at a time (a combined search hid her CDSL statements). CDSL's monthly CAS (`…_TXN.pdf`) is the independent holdings check (both PANs: all funds in demat, no MF folios). KFintech/CAMS send no personal statements any more: the KFintech accounts are old data.
- Returned cheques (father's) are transfers; P2P (Innofin) repayments are transfers; the household's own name in an alert means a transfer.
- Net worth history starts 31 Mar 2021 (first dated holdings); the flat is owned from its loan's start (15 Jan 2024); EPF/NPS before their anchor date show the anchor balance.

## Commands
- Tests + live checks: `scripts/check.sh --live` (drops and recreates the scratch DB `kutumbh_test`). If the sandbox refuses the DROP: `CREATE DATABASE kutumbh_audit_scratch`, run `docker run --rm -i --network kutumbh_default -v "$PWD":/app -w /app -e TEST_DATABASE_URL=postgresql://…/kutumbh_audit_scratch kutumbh-app python -m unittest discover -s tests -t .`, then drop it. After an intended move of figures: `scripts/figures.py --accept` (baseline is git-ignored).
- Health: `GET /api/health/consistency` (expect 0 failing; one warning while the wife's EPF balance is over a year old), `/api/health/alerts`.
- Redeploy: the image bakes the code. Make a Dockerfile with `FROM kutumbh-app:latest` / `COPY . .`, `docker build -t kutumbh-app:latest`, `docker compose up -d --no-deps app` (wait ~20 s). Ad-hoc scripts against the live DB: `docker run --rm -i --network kutumbh_default -v "$PWD":/app -w /app --env-file .env -e DATABASE_URL="$(docker exec finance_app printenv DATABASE_URL)" -e PYTHONPATH=/app kutumbh-app python - < script.py` (session = `app.models.database.SessionLocal`). After a change that moves net worth or goals: `snapshot_all` + `refresh_goals`, or `POST /api/goals/refresh-corpus`, then the health check. `backfill_snapshots(db, force=True)` rebuilds every stored day.
- Backups: `data/backup/*.dump` (latest `pre_merge_health_20261024.dump`).
- Never bump `_CACHE_SCHEMA` (hours to re-parse); bump the parser's own `_PARSER_VERSION`. Check `demat_movements` and every FK before deleting an account. Never write a file with `open(p,'w').write(open(p).read())` (it truncates).

## State on 2026-10-24
Family net worth about ₹3.53 cr. Emergency ₹21.13 L (bank ₹2.15 L + FD/RD ₹18.98 L) against a derived ₹30.66 L (short about ₹9.5 L). Retirement & Senior Health about 89% with no more saving (both EPFs, VPF, NPS and the return of premium carry it). Education and marriage far from funded; suggested SIPs education about ₹1.18 L, marriage about ₹32k, retirement 0. Actual SIPs: education ₹49.5k (PPFAS, her MOMF and Quant, ICICI Large Cap), marriage ₹10k (UTI Nifty Next 50). Capacity about ₹2.1 L a month (loan ends 2033-05). Silver ETFs (₹2 L, at a loss) are flagged as a bet and counted toward no goal.

## Open items (also in `docs/ROADMAP.md`)
1. Review list (a handful of payees to label); the wife's EPF balance is over a year old (share a newer one about yearly; also her NPS balance if she can see it).
2. A standing CAS reconciliation check (each CDSL CAS against the ledger's units on its date, shown on the Accounts page). Checked by hand for 30 Sep 2026: it reconciles.
3. A derived diversification insight to replace the removed fixed-target recommendations.
4. Net worth before Mar 2021 could be rebuilt from 2018-21 trades plus NAV/price history; not built.
5. The owner has no health policy beyond employer cover (assumed ₹10 L individual policy at retirement, `params.yml`) and no pension for the wife counted.
6. Fund data sources are public but unofficial (Groww page JSON); the regression is the fallback. AMFI, mfapi.in, Yahoo, FRED, RBI/World Bank feeds are cached; failures degrade, none invent numbers.
