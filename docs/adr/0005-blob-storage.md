# ADR-0005: Blob storage — local filesystem (`./data/raw`)

**Status:** Accepted
**Date:** 2024-04

## Context

Raw attachments (PDFs, CSVs) need to be persisted so parsers can be re-run after improvements without re-downloading from IMAP. Volume is small: tens of MB per year.

## Options

- **S3 / MinIO** — durable, but introduces an external service and breaks the "no third-party services" goal.
- **Postgres LOBs** — keeps everything in one place but Postgres is bad at large blob storage and clobbers the WAL.
- **Local filesystem bind-mounted into the app container** — trivial, durable on the host disk, easy to backup with `rsync`.

## Decision

Bind-mount `./data` into the app container at `/app/data`. Raw attachments go into `./data/raw/<institution>/<yyyy>/<mm>/<filename>`.

## Consequences

- ✅ `rsync` to a backup disk is the entire backup story for raw inputs.
- ✅ Re-running a new parser on history is `for f in ./data/raw/hdfc/**/*.pdf; do ... done`.
- ❌ Single-host only. Acceptable; the whole project is single-host.
- ❌ Disk encryption depends on the host (FileVault / LUKS). Documented in PRODUCTION_READINESS.md §3.
