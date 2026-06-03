# ADR-0010: Grafana as the frontend

**Status:** Accepted
**Date:** 2024-04

## Context

The user needs a visual dashboard: net worth over time, cash flow, holdings, goals. Two paths: build a custom React/Next app, or use Grafana.

## Options

- **Custom React/Next + Recharts** — pixel-perfect; weeks of work; auth/templating/alerting all DIY.
- **Apache Superset** — heavier than Grafana; better for slice-and-dice exploration but its dashboarding ergonomics are worse for personal use.
- **Metabase** — nice but its embed model and provisioning story are weaker than Grafana's.
- **Grafana** — provisioning-as-code, templating, alerting, mobile-web all included. Free Postgres datasource.

## Decision

Grafana 11 with everything provisioned from `grafana/provisioning/` and `grafana/dashboards/`.

## Consequences

- ✅ Four dashboards shipped: Net Worth, Cash Flow, Investments, Goals & Planning.
- ✅ Every panel is plain SQL — forces clean DB semantics and keeps logic out of the visual layer.
- ✅ Alerts ("no sync ran in 24h", "EMI > 40% of income") come for free.
- ❌ Tightly coupled multi-panel UX (e.g. master-detail flows) is awkward. Not needed at this scale.
- ❌ The repo carries a few large JSON dashboard files; diffs are noisy. Mitigated by treating them as artifacts, not source code (edit in UI → export → commit).
