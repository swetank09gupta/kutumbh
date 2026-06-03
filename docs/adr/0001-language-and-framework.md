# ADR-0001: Language and framework — Python 3.12 + FastAPI

**Status:** Accepted
**Date:** 2024-04

## Context

The system has to parse PDF/CSV bank statements, talk IMAP, expose a small REST API, and run a few scheduled jobs. The author is the only operator.

## Options

- **Python + FastAPI** — best-in-class PDF / email / pandas ecosystem; FastAPI gives typed routes + free OpenAPI.
- **Go + chi** — fastest deploys, single static binary; ecosystem for PDF/email is thin.
- **Node + Fastify** — JS pdf libs exist but are weaker than `pdfplumber`/`PyPDF2`; date handling is painful.
- **Java + Spring** — solid PDF (Apache PDFBox) but ceremony overhead is wrong for a one-person project.

## Decision

Python 3.12 with FastAPI.

## Consequences

- ✅ `pdfplumber`, `mailparser`, `pandas`, `sqlalchemy`, `apscheduler` cover ~90% of the work out of the box.
- ✅ FastAPI's typed Pydantic models give us free request validation and `/docs`.
- ✅ Iteration speed is the dominant factor; CPython is fast enough at this workload.
- ❌ Single-threaded by default; we run uvicorn with one worker. Not an issue at household scale.
- ❌ Distribution is a Docker image, not a static binary. We need Docker anyway for Postgres/Grafana/Ollama, so this is free.
