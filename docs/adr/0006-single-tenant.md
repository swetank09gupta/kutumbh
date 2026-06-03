# ADR-0006: Single-tenant by design

**Status:** Accepted
**Date:** 2024-04

## Context

Should the data model support multiple households per deployment?

## Decision

No. One deployment = one household. Multiple **owners** (self, spouse, parents, children) are first-class — every monetary row has `owner_id`. Multiple **households** are not supported.

## Consequences

- ✅ The data model is dramatically simpler. No tenant_id everywhere, no row-level security policies, no cross-tenant leakage tests.
- ✅ Trust model is "everyone with shell access to the host can see everything." Realistic for a household NAS.
- ❌ Cannot share a hosted instance with friends. The expected deployment is one Docker stack per family, on each family's own hardware.
