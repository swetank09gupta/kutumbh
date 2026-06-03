# ADR-0011: Categorisation via regex rules, not ML

**Status:** Accepted
**Date:** 2024-04

## Context

Each bank transaction needs a category: salary, emi, sip, groceries, utilities, …

## Options

- **ML classifier** (sklearn / fasttext / fine-tuned small LM) — would generalise to unseen merchants; needs labelled data and retraining loops.
- **Regex rules in code** — explicit, debuggable, deterministic; edits ship in seconds.
- **LLM at categorisation time** — slow and overkill; the LLM is already paying for itself on parsing fallback.

## Decision

A flat ordered list of `(pattern, category, merchant)` tuples in `app/etl/categorizer.py`. ~50 rules cover >95% of household rows.

## Consequences

- ✅ When a transaction is mis-categorised, fixing it is editing one line, then re-running the categoriser (idempotent).
- ✅ No training data, no model file, no inference latency.
- ✅ The "Unknown category %" Grafana panel is the leading indicator of when new rules are needed.
- ❌ Doesn't generalise — every new merchant is a manual rule. Acceptable: at household scale, a few rules per month covers the long tail.
