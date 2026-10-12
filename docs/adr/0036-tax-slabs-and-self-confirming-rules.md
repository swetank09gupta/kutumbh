# ADR-0036: Slab tax is estimated per person; rules confirm themselves against their source

**Status:** Accepted
**Date:** 2026-10

## Context
Total income per person is not in the system, but each person's salary is what the bank shows credited; anything extra is non-taxable. Separately, rules kept "by hand" (ADR-0027) cannot be maintained manually for years.

## Decision
- **Slab estimator** (`app/knowledge/tax_slabs.yml`, `app/engine/tax_slabs.py`, `tax_view.estimate`): per regime and FY — slabs, standard deduction, 87A rebate, 4% cess, no surcharge. Income = salary credited + proven interest, dividends, SGB coupons; old regime deducts 80C/24(b) claims (loan certificates) and 80TTA. Indicative total = slab tax + special-rate (capital gains) tax × 1.04. It is a **floor**: salary is net of the employer's TDS and missing statement months are missing. It says so.
- **Rule probes:** a rule may carry `probe` (regex) and `probe_url`; `live.verify_sources` re-reads the page daily and records a confirmation, so `verified_on` advances without a person. Staleness: 3 days for live feeds, 10 days for probed rules, quarter-start/120 days for the rest. A page that stops confirming turns into the usual warning.
- Probes **confirm** a value; they cannot **discover** a Budget change. Each April a `tax_slabs.yml` entry for the new FY is needed; `tax_slabs_are_current` warns until it exists.

## Consequences
Tests: `test_tax_slabs`, `test_automation`, `test_dividends_tax_db`; checks `tax_slabs_are_current`, `rules_are_current` (uses probed ids).
