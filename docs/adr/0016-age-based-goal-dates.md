# ADR-0016: Age-based goal target dates

**Status:** Accepted
**Date:** 2026-06

## Context

Each `Goal` row carries a `target_date` set when the goal was created. For some goal types — retirement, child_education, child_marriage — that date should track the relevant person's life stage. A 32-year-old who declared `retirement_age = 50` should be pacing a 16-year horizon to 2042, not whatever static 2050 was in the seed config.

In the reference dataset this discrepancy was 8 years on the user's retirement goal — enough to halve the required SIP and produce a wildly optimistic "on track" picture.

## Options

1. **Leave it static** — user must keep `target_date` in sync with their age. Brittle; nobody does.
2. **Mutate the goal row on each refresh** to overwrite `target_date` from profile data. Wins on "single source of truth" but trampling user edits and losing audit trail is bad.
3. **Compute effective target_date at projection time** from profile data when available; keep the stored `target_date` as a fallback.

## Decision

Option 3. `_age_target_date(goal, db)` returns:

- `retirement` → `owner.birth_date + owner.retirement_age` years.
- `child_education` → `child.dob + child.education_age` (default 18) from the optional `children:` block in `config/accounts.yml`. With multiple children, the **oldest** child wins (earliest funding need).
- `child_marriage` → same with `marriage_age` (default 25).
- Anything else (or missing profile data) → `goal.target_date` unchanged.

Children stay in YAML config, not the DB — no schema migration, no UI to add for now. Per-child goals can be added later if/when the user wants distinct rows.

## Consequences

- ✅ Stored `target_date` becomes a fallback / human-readable reference, not the source of truth.
- ✅ Updating retirement age in the Profiles UI immediately re-paces SIP across every projection refresh.
- ✅ No migration: existing `target_date` values are still respected when profile data is missing.
- ❌ Two-source-of-truth surface: a user could see "Your Retirement: 2050-01-01" in the database and "2042-03-09" in the projection. Mitigation: the projection response includes the *effective* `target_date`, so the UI shows the age-derived one.
- ❌ Children in YAML can drift from reality (no birthdays update themselves). Acceptable: one-line edit, restart not needed (config is re-read on each projection call).
