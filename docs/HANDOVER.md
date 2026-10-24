# Handover: where things stand (updated 2026-10-21)

Read this first, then `CLAUDE.md` (working agreement), `docs/INVARIANTS.md`, `docs/DECISIONS.md`. Latest decisions: ADR-0043 (one calculator), 0044 (matching), 0045 (personal facts), 0046 (audit), 0047 (retirement + senior health), 0048 (SBI renewals), 0049 (UI audit).

## What the app is
Family finance on FastAPI + SQLAlchemy + Postgres (docker compose), one-page UI `app/static/index.html`, APScheduler jobs (sync from mail, daily sweep). Every page reads shared definitions: `v_cashflow`, `latest_holdings_deduped`, `build_positions`, `project_all_goals`. Private config in `config/accounts.yml` (git-ignored; never print it).

## Goals: how a card is made
- `engine/goals.project_all_goals` is the only calculator. `planning/goal_plan.overlay` fills target, SIP for 80%, chance, return, inflation from market data (`app/planning/`, params in `params.yml`).
- Corpus (`refresh_corpus_from_networth`): emergency = bank + live FD/RD; others = everything tagged (`goal_plan.holdings_by_owner`). Tags: manual (UI, never moved) or auto (`planning/matching.py`, after sync and in the daily sweep). Deposits are always the emergency fund.
- Funding order and notes: `goal_plan.apply_funding` (emergency, then by date; keeps working SIPs). Details: `docs/PLANNING.md`.
- Personal facts in config: EPF, pay, family history, term plan (return of premium), loan shares, `goal_priority`.

## Commands
- Tests + live checks: `scripts/check.sh --live` (drops and recreates the scratch DB `kutumbh_test`; if the sandbox refuses the DROP, create a scratch DB and run `docker run … -e TEST_DATABASE_URL=… kutumbh-app python -m unittest discover -s tests -t .`). After an intended move: `scripts/check.sh --accept`.
- Health: `GET /api/health/consistency` (0 failing, 0 warnings expected), `/api/health/alerts`.
- Redeploy: the image bakes the code; build `FROM kutumbh-app:latest / COPY . .` then `docker compose up -d --no-deps app`. After a change that moves net worth or goals: run a snapshot + `refresh_goals`, or `POST /api/goals/refresh-corpus`, then check.
- Backups: `data/backup/*.dump` (latest `pre_audit_20261021.dump`).
- Never bump `_CACHE_SCHEMA` (hours to re-parse); bump the parser's own `_PARSER_VERSION`.

## State on 2026-10-21
Family net worth ₹3.10 cr. Emergency ₹21.14 L (bank ₹4.66 L + FD/RD ₹16.48 L) vs ₹33.91 L derived (short ₹12.8 L). Retirement ~99% chance with no more saving (both EPFs, NPS and the return of premium carry it): its holdings are moved to education and marriage by the matcher; capacity ₹2.1 L/month (loan ends 2033-05); The goal is now one "Retirement & Senior Health" (ADR-0047): 92% with no SIP, funded by EPF, VPF and NPS contributions that grow with salary; suggested SIPs education ₹1.18 L, marriage ₹32k, retirement 0, emergency a one-off ₹12.8 L; actual SIPs today education ₹39.5k, marriage ₹10k (UTI Nifty Next 50, manual tag), retirement ₹10k (ICICI Large Cap, could go to education) (the app only suggests; the mandates are yours). Silver ETFs flagged as a bet. Health 0 failing.

## Open items (also in `docs/ROADMAP.md` Inbox)
1. SBI savings balance = alert print minus the owner's live SBI deposits (ADR-0046 correction); a new statement should confirm it. (The wife's duplicate SBI 1636 row was deleted.)
2. Wife's EPF (anchor 31 Mar 2025, rolled forward) and NPS (contributions only) are in. Share a newer EPF balance for each person about yearly (warning after 13 months).
3. A derived diversification insight to replace the removed fixed-target recommendations.
4. Goal priority between education and marriage: default is date order; set `goal_priority` to change.
5. SBI e-mails no alert for UPI payments or SIP/mandate debits: those rows appear in the Transactions list only when the statement arrives (balances are unaffected: the bank's printed balance less its live deposits). Zerodha payment mails fill in the transfers to the broker.
6. Fund data sources are public but unofficial (Groww page JSON); the regression is the fallback. AMFI catalogue, mfapi.in, Yahoo, FRED, RBI/World Bank feeds are cached; all failures degrade, none invent numbers.
