# ADR-0021: Employer salary patterns in config, not source

**Status:** Accepted  
**Date:** 2026-06

## Context

The transaction categoriser needs to recognise salary credits from the employer NEFT narration. Bank narrations often contain the employer name directly (e.g. `NEFT FROM ACMECORP`) without the word "salary", so a generic rule is insufficient.

The initial implementation hardcoded employer name fragments as regex literals inside `app/etl/categorizer.py`. Since this repository is public, those literals revealed the author's and spouse's employers — personally identifiable information that should not appear in source code.

## Decision

Employer-specific salary patterns are read at runtime from `config/accounts.yml` (which is git-ignored) rather than being hardcoded in source.

Two configuration styles are supported, ordered by ease of use:

1. **`salary_keywords`** — plain strings (e.g. `"acmecorp"`). Automatically escaped and compiled to a case-insensitive regex. No regex knowledge required. This is the recommended path for most users.

2. **`salary_patterns`** — raw Python `re` expressions for complex narration patterns (e.g. `neft.*acme|acme.*neft`). For power users only.

Both are per-owner lists and may coexist. A generic fallback rule (`salary|sal credit|payroll|neft.*sal|ctc`) in the categoriser handles the majority of cases where neither list is configured.

Pattern loading is fail-safe: any exception during config read returns an empty list, so categorisation degrades gracefully (generic rule still runs) rather than crashing.

## Alternatives considered

- **Environment variables** — too verbose for multi-owner households; env vars don't naturally express a per-owner list.
- **Database table** — adds schema complexity for what is effectively startup configuration; config file is simpler and consistent with how other per-owner fields (EPF, home-loan share) are handled.
- **Comments-only redaction** — removing the literals from a comment while keeping them in a regex still exposes them. The pattern itself must leave source.

## Consequences

- The public repository contains no employer names, PAN numbers, or other PII.
- Users who previously had no `salary_keywords`/`salary_patterns` in their config will have salary credits fall through to the generic `salary|payroll` rule — no categorisation regression for standard HDFC/SBI narrations.
- Users with unusual narrations (employer name only, no "salary" word) must add `salary_keywords` to their local `accounts.yml` — documented in [CONFIGURATION.md](../CONFIGURATION.md) and [TROUBLESHOOTING.md](../TROUBLESHOOTING.md).
