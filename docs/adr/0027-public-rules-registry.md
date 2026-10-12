# ADR-0027: Public rules live in one effective-dated registry

**Status:** Accepted
**Date:** 2026-10

## Context
Limits, rates and thresholds set by Parliament, the RBI or the Government (PPF limit, 80C cap, TDS thresholds, capital-gains rates, MF stamp duty, EPF rate, repo rate) were typed into code (`Decimal("150000")` twice, a default PPF 7.1%). They change by date, so a typed value silently goes wrong, and history dates need the value in force *then*.

## Decision
`app/knowledge/rules.yml`: one entry per value per date range (`id`, `value`, `unit`, `effective_from`, `source` URL, `verified_on`, optional `note`). A change adds a later entry; the old one stays. `app/knowledge/rules.py`: `value(id, on=date)`; a date before the first entry raises `RuleNotInForce` (no guessed history). A rule enters the file only after it is written up with its source in `docs/INSTRUMENTS.md`.
Live feeds stay where they are (PPF rate table, CPI, NAV). The PPF default rate now comes from that table, not a constant.
**Staleness guard:** consistency warning `public_rules_are_current` when a rule in force was verified over 120 days ago or before the current quarter start. Clearing it means re-reading the source and bumping `verified_on`.

## Consequences
No figure moved (checked live: 80 figures). `tests/test_rules.py` checks shape, no overlap, lookup on the effective day, staleness, and that the old literals do not come back. Later phases (deadlines, tax lens, loan forecast) read their parameters from here. Entries are added per phase, only when verified.
