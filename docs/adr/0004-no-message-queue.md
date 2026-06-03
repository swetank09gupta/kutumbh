# ADR-0004: No message queue — APScheduler in-process

**Status:** Accepted
**Date:** 2024-04

## Context

Statement emails arrive ~daily; sync runs every 6h. Across 5 mailboxes that's <50 events/day. Nothing in the workload is latency-sensitive.

## Options

- **Kafka / Redpanda** — overkill; another container to operate; durability we don't need.
- **Redis Streams** — lighter than Kafka but still a moving part; no consumer-group story we need.
- **Celery + Redis broker** — common Python choice; adds a worker container.
- **APScheduler in-process** — runs inside the same FastAPI container; persists job state to Postgres.

## Decision

APScheduler in-process with a Postgres job store.

## Consequences

- ✅ One fewer container. The whole "background work" subsystem is ~15 lines in `app/scheduler.py`.
- ✅ Manual triggers via `POST /api/sync/now` are just function calls in the same process.
- ❌ If the FastAPI container dies mid-sync, the run is aborted; the next scheduled tick will pick up the same emails (IMAP UIDs are sticky and `email_seen` is the dedup layer).
- ❌ No horizontal scaling. Not a goal.
