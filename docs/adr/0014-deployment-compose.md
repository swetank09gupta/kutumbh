# ADR-0014: Deployment — docker-compose on a single host

**Status:** Accepted
**Date:** 2024-04

## Context

The target deployment is a household: a Mac mini, a Raspberry Pi NAS, or a small Linux box. Five services need to come up together with shared networking and volumes.

## Options

- **Docker Compose** — one YAML, one command, deterministic.
- **Kubernetes (k3s/minikube)** — wildly overkill; adds an entire orchestration surface for five containers on one host.
- **systemd units** — works but volume + network plumbing is more YAML elsewhere, and the user loses image versioning ergonomics.
- **Nix** — clean but adoption cost is high for the target audience.

## Decision

Docker Compose v2 with a single `docker-compose.yml` at the repo root. Five services: `postgres`, `app`, `ollama`, `ollama-init` (one-shot), `grafana`. Named volumes for each piece of persistent state.

## Consequences

- ✅ `docker-compose up --build -d` is the entire install. `down` is the entire uninstall.
- ✅ Healthchecks wired so `app` waits for Postgres and `ollama-init` waits for Ollama.
- ❌ Not HA. Not auto-scaling. Not the goal.
- ❌ Updating the stack is "git pull && docker-compose up --build -d". No blue-green. Acceptable for the household use case.
