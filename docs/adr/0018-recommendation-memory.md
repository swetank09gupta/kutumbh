# ADR-0018: Recommendation memory and change detection

**Status:** Accepted
**Date:** 2026-06

## Context

After the recommendation engine grew to ~12 active checks (ADR-0017 added 5 portfolio-quality checks and 4 market-timing recs to the original 6), the user raised a pointed concern: investments take years to appreciate, but recommendations recompute every dashboard load. Without memory, an actively-followed recommendation looks identical to one being ignored — both render every refresh. There's no way to tell "the user already started a Nasdaq 100 SIP last month, the international-gap warning should be soft now" from "they've been told 6 times and haven't acted".

A stateless engine also can't show *trend* — was the midcap overweight getting better or worse? Even when the rec persists, the underlying metric (midcap_pct) might be moving in the right direction.

## Options

1. **Stay stateless.** Acceptable for a v1 recommendation engine; explicitly accept the noise vs signal problem. Already what we had.
2. **Heuristic suppression.** When a recommendation appears for the Nth refresh, downgrade priority. Solves visual fatigue but doesn't expose *what changed* and can hide genuinely important persistent recs.
3. **Persistent snapshots + diff.** Write today's recommendations to a table, diff against the most-recent snapshot 7-35 days ago, surface new / resolved / persisting buckets to the UI.

## Decision

Option 3. Add a `recommendation_snapshots` table and an idempotent `snapshot_recommendations()` helper that writes one row per `(owner_id, fingerprint, snapshot_date)`. Subsequent loads on the same day update the existing row instead of duplicating — so dashboard refreshes are free.

`fingerprint = "{category}|{title}"`. Detail and action text drift across snapshots (₹ amounts change as the portfolio grows) but the fingerprint stays stable, so lifecycle tracking is meaningful. Each rec also persists a `metrics` JSON dict — for the quality checks, this is `{"midcap_pct": 26.1, "equity_total": 3246595}` etc. The diff API reads back the primary numeric metric from each snapshot in the lookback window and returns a trend list, which the UI renders as `↓ midcap_pct: 26.0 → 22.0` in green when the metric is moving the right way.

The diff endpoint (`/api/goals/recommendations/changes`) returns three buckets:

- **`new`** — fingerprints active today, absent in the prior snapshot. Highlights what the latest sync surfaced (e.g. user just sold a large-cap fund → asset-allocation drift fires for the first time).
- **`resolved`** — active before, absent today. Strong signal that the user acted (or the underlying metric drifted out of the threshold zone naturally).
- **`persisting`** — active in both, with `count_snapshots`, `since_date`, and the metric trend. Drives the visual cue "this has been around for 4 months; act on it or dismiss it".

The snapshot write is fire-and-forget: any commit failure is logged but the recommendations endpoint still returns its payload. Snapshot integrity isn't on the critical path.

## Consequences

- **Trustworthy advice.** A rec that's been suggesting "Add international exposure" for 5 months with `intl_pct: 0 → 0 → 0` is visually distinct from one where `intl_pct: 0 → 2.1 → 4.8` showing progress.
- **Cheap storage.** ~15 rows/day × 365 = ~5.5k rows/year. Negligible.
- **No automatic suppression.** A "persisting" rec doesn't fade automatically — that's a deliberate choice. Suppression would hide legitimately important recurring advice. The visual treatment (separate bucket, count badge) is what nudges action.
- **Owner-scoped.** Snapshots store `owner_id` (default `"family"`) so per-owner views work, but the v1 UI only renders the family aggregate.
- **No back-fill.** Past months can't be reconstructed — the table only grows from deploy date forward. Documented as a known limitation in the UI ("first snapshot — come back in a week to see changes").

## Future work

- **Action capture.** A "Mark as acted" button on each rec that records an explicit suppression for N days. Cleaner than relying on the metric to drift naturally below threshold.
- **Per-owner views.** Currently family-level only; spouse-specific recs (e.g. ELSS Dead-Weight per owner) write a single family snapshot row each.
- **AI advisor with memory.** The `persisting` bucket is a perfect prompt input for the Llama-based advisor — "the user has been told X for 5 months, summarise as a short nudge". Out of scope for this ADR.
