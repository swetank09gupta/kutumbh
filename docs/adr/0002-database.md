# ADR-0002: Database — PostgreSQL 16

**Status:** Accepted
**Date:** 2024-04

## Context

We need a relational store with transactions, sane date/time handling, JSONB for parser metadata, and a Grafana datasource. The dataset is small (≤100k rows over a decade).

## Options

- **PostgreSQL** — full-featured, native Grafana support, JSONB, window functions, timezones done right.
- **SQLite** — zero-ops, file-on-disk; Grafana has no first-class SQLite datasource; concurrent writes from scheduler + API are awkward.
- **DuckDB** — excellent analytics but write-side and concurrency story is wrong for a long-lived service.
- **MongoDB** — schemaless flexibility we don't need; SQL queries in Grafana panels would be lost.

## Decision

PostgreSQL 16 (alpine image).

## Consequences

- ✅ Every Grafana panel is plain SQL. No ORM in the dashboard layer.
- ✅ `TIMESTAMP WITH TIME ZONE` + `TZ=Asia/Kolkata` + `PGTZ=Asia/Kolkata` gives correct end-of-month behaviour.
- ✅ JSONB is used in a couple of places (parser raw output) without committing to schemaless everywhere.
- ❌ Heavier than SQLite at idle (~180 MB RSS). Acceptable for a home server.
- ❌ Schema migrations need a tool. We use Alembic.
