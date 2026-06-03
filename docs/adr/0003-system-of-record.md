# ADR-0003: System of record — Postgres, with emails as upstream source

**Status:** Accepted
**Date:** 2024-04

## Context

Two candidate sources of truth: the original email mailbox, or the parsed/normalised database. We need both for different reasons.

## Decision

- **Mailbox = canonical upstream source.** It's immutable, persists independently of this software, and contains the original PDFs/CSVs.
- **Postgres = system of record for the app.** Every dashboard and computation reads from Postgres.

The DB is treated as a **rebuildable cache**: dropping it and re-running `POST /api/sync/historical?since_days=N` reconstructs identical state.

## Consequences

- ✅ Disaster recovery is "delete `postgres_data` volume, re-sync." No backup of derived state is strictly required (though we still snapshot it — see `deploy/BACKUP.md`).
- ✅ Migrations can be destructive — re-derive on the new schema.
- ✅ Parsers can be improved and the corrected history retro-applied.
- ❌ Anything the user enters manually (PPF, FD, NPS, real estate) is **not** in the mailbox. That data lives only in Postgres and must be backed up. The `manual_assets` table is small (~20 rows for a typical household) and exported nightly to `./data/raw/manual_assets.csv`.
