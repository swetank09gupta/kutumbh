# ADR-0009: Deterministic parsers first; LLM only as fallback

**Status:** Accepted
**Date:** 2024-04 (reversed an earlier "LLM-first" prototype)

## Context

Early prototype piped every statement to the LLM. It worked but: 60–90s per statement, ~0.5% hallucination on dates/amounts, unit-testable only via fragile snapshots.

## Decision

- **Tier 1:** institution-specific deterministic parsers (`app/ingestion/parsers/*.py`).
- **Tier 2:** if a parser raises or returns zero rows, the raw text is passed to `llm_extractor` with a strict JSON schema prompt.
- Rows from tier 2 are tagged `source='llm'` and surfaced on the Grafana "Parser health" panel for review.

## Consequences

- ✅ ~95% of statements processed in <1.5s with no AI involvement.
- ✅ Adding a new bank is "write a parser", and parsers are unit-testable with fixture PDFs.
- ✅ The LLM tier is the safety net: a new statement layout doesn't break Sunday-morning sync.
- ❌ Maintenance work shifts onto the parsers. Worth it — the per-parser test fixtures catch regressions in seconds.
- ❌ Two code paths to reason about. Mitigated by the parser-health panel making tier-2 visible.
