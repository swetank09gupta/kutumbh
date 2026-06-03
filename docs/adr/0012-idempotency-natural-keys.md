# ADR-0012: Idempotent ingestion via natural keys

**Status:** Accepted
**Date:** 2024-04 (replaced an early UUID-PK design)

## Context

Bank statements overlap. The user will re-run `POST /api/sync/historical` after parser improvements. The same transaction can appear in multiple monthly PDFs (e.g. running balance carry).

## Decision

Every `transactions` row has a natural key:

```
(account_id, posted_on, amount_paise, narration_hash)
```

where `narration_hash = sha1(normalize(narration))` and `normalize` strips reference numbers that change between exports, collapses whitespace, and lowercases.

A unique index on this tuple turns re-ingestion into an `ON CONFLICT DO NOTHING`. Surrogate UUID PKs are kept too, but only as a join target.

## Consequences

- ✅ Re-running ingest is a guaranteed no-op on unchanged input. Verified in tests.
- ✅ Parser improvements that re-extract the same row don't create duplicates.
- ❌ A genuine duplicate transaction in real life (same account, day, amount, narration) collapses into one row. So far this has not happened with real data; if it does we'd append a discriminator to the narration before hashing.
- ❌ The `normalize` function is load-bearing — changes to it require a one-shot re-hash migration.
