# ADR-0039: Missed jobs run once on wake; a stale snapshot is a warning, not a failure

**Status:** Accepted
**Date:** 2026-10

## Context
The app runs on a laptop that sleeps. On 18–19 Oct the 00:05 snapshot was "missed by 23:07" and the 6-hourly sync "missed by 0:38"; APScheduler dropped both (grace 1 s for cron jobs, 300 s for the sync) and the data stayed a day old. ADR-0029's catch-up only fires after a container restart, not after a sleep.
A day-old snapshot then showed three red failures (returns vs net worth, INV-03/INV-09, stored vs live goal SIP). The data was right: deposits accrue interest by the day and are valued live, while net worth and the stored goal SIP were taken at the last update, so they differed by one day's accrual (₹108 on ₹6.35 lakh).

## Decision
- **Every job coalesces** (one catch-up run, not one per missed tick) and has a grace that survives a night: 23 h for the interval and daily jobs, 6 days for the weekly goal job.
- **A watchdog (every 10 min)** runs the update when none has been *attempted* for the interval + 30 min, and does nothing while an update runs. Failed attempts count, so a broken mailbox retries once per interval. It covers sleeps longer than any grace. The update refreshes statements, snapshot and goals together.
- **Stale snapshot (`consistency.stale_snapshot`):** while the newest snapshot is older than today, a failing deposit/PPF-vs-snapshot comparison (`returns_match_holdings`, INV-03/INV-09 lines about fd/ppf/deposits) or stored-vs-live goal SIP is reported as a **warning** that names the snapshot date, provided the difference is no larger than `within_accrual` (0.1% per stale day, 0.5% for SIP). Anything bigger, any equity/fund mismatch, and any mismatch on a current snapshot stays a failure. The missing update itself is reported by `automatic_update_is_running`.

## Consequences
Tests: `tests/test_automation.py` (`MissedJobs`, `StaleSnapshotIsAWarningNotAFailure`). Revises the "Revisited (Oct 2026)" note of ADR-0029: catch-up after restart stays; sleeping is now covered too.
