# Architecture Decision Records

Each ADR captures one decision: the context, the options considered, the choice, and the consequences. Format roughly follows Michael Nygard's template.

| # | Title | Status |
|---|---|---|
| [0001](0001-language-and-framework.md) | Language and framework: Python 3.12 + FastAPI | Accepted |
| [0002](0002-database.md) | Database: PostgreSQL 16 | Accepted |
| [0003](0003-system-of-record.md) | System of record: Postgres, with emails as upstream source | Accepted |
| [0004](0004-no-message-queue.md) | No message queue — APScheduler in-process | Accepted |
| [0005](0005-blob-storage.md) | Blob storage: local filesystem (`./data/raw`) | Accepted |
| [0006](0006-single-tenant.md) | Single-tenant by design | Accepted |
| [0007](0007-no-auth-on-lan.md) | No app-level auth; reverse-proxy if exposed | Accepted |
| [0008](0008-llm-locality.md) | LLM runs locally via Ollama | Accepted |
| [0009](0009-parsers-before-llm.md) | Deterministic parsers first; LLM only as fallback | Accepted |
| [0010](0010-grafana-as-frontend.md) | Grafana as the frontend | Accepted |
| [0011](0011-regex-categorisation.md) | Categorisation via regex rules, not ML | Accepted |
| [0012](0012-idempotency-natural-keys.md) | Idempotent ingestion via natural keys | Accepted |
| [0013](0013-custom-xirr.md) | Custom XIRR (Newton–Raphson) over numpy-financial | Accepted |
| [0014](0014-deployment-compose.md) | Deployment: docker-compose on a single host | Accepted |
