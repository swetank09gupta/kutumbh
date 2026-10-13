# ADR-0029: Everything that can update by itself does, through one pipeline

**Status:** Accepted
**Date:** 2026-10

## Context
Only statements were fetched on the 6-hourly schedule. Bank alert e-mails and HDFC loan letters were read only when the Sync button was pressed, so the alerts card and the loan balance silently went stale. The sequence "maintenance → ledger → snapshot → goals" was copied into the scheduler, the Sync route and reprocess. Public rules (ADR-0027) were hand-verified only.

## Decision
- `app/etl/pipeline.py` is the one definition: `ingest_evidence` (alerts, loan letters, public rules; each step isolated) and `rebuild` (maintenance → ledger → snapshot → goals → consistency). The scheduler, the Sync button and reprocess all call it; a lock keeps a scheduled run and a manual one from overlapping.
- `app/knowledge/live.py` reads the RBI repo rate from rbi.org.in daily (also in every update) and stores it as `rule_observations`. They are laid over `rules.yml`: a value that agrees only refreshes `verified_on` (clearing the stale warning for that rule); one that differs becomes a new entry starting the day it was first seen, the old one kept for earlier dates. An unreachable or unreadable page writes nothing. A rule that updates itself must keep doing so: `public_rules_are_current` warns when it has not been read for 3 days (page down or redesigned), instead of waiting for the quarter. Rules with no official machine-readable page (tax thresholds, PPF limit, EPF rate) stay hand-verified, and the quarterly warning stays for them.
- `automatic_update_is_running` (warning) shows when the last update is older than 3 intervals or failed, so a stopped scheduler is visible.
- Found by running the scheduled path on live data: a Coin order whose ISIN a learned alias had corrected in place (natural key unchanged) was inserted again by the next 7-day rescan. Coin orders are now identified by owner, folio, date, side, units and amount regardless of ISIN, and a maintenance step removes existing copies. A 6-hourly rescan makes this class of bug matter.

## Consequences
New table `rule_observations`; scheduler gains `live_rules` (07:10). `tests/test_automation.py`, `tests/test_coin_duplicates_db.py` (the second fails without the fix). No figure moved; one duplicate trade (+₹10k) removed.

**Revisited (Oct 2026):** the scheduler keeps no memory across restarts, so jobs due while the laptop was off (00:05 snapshot, 07:10 rules) were skipped and the 6-hourly sync waited a full interval after boot. The app now runs the rules refresh (+30 s) and the sync (+90 s, window = since the last good run) once after every start. A restart showed as net-worth vs Investments mismatches (daily deposit accrual) until the next rebuild.
