# ADR-0007: No app-level auth; reverse-proxy if exposed

**Status:** Accepted
**Date:** 2024-04

## Context

FastAPI and Grafana both ship auth machinery. Do we use it?

## Decision

- **FastAPI:** no auth on the application layer. The API is bound to the Docker network only by default; the only host port exposed is `8000` and the assumption is it sits behind the LAN.
- **Grafana:** keeps its built-in admin auth (forced via `GF_SECURITY_ADMIN_PASSWORD`). Anonymous "Viewer" role is enabled for read-only convenience on the LAN.
- **If exposed to the internet:** the user is expected to put a reverse proxy (Caddy, nginx, Tailscale Funnel) in front with HTTPS + basic auth or OIDC. A sample Caddyfile is in `deploy/`.

## Consequences

- ✅ Zero auth code in the app. No JWT, no session store, no password reset flow.
- ✅ Clear boundary: anyone on the LAN with the URL is trusted. Same threat model as the household NAS itself.
- ❌ Users who expose this carelessly to the internet have no in-app safety net. Documented loudly in the README.
