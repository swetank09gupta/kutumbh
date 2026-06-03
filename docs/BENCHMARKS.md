# Benchmarks

Numbers from a 2024 M2 MacBook Air (8 GB), Docker Desktop with 4 CPUs / 6 GB RAM allocated. Mileage on a Linux NAS will differ; the orders of magnitude won't.

## Ingestion

| Step | Volume | Wall time |
|---|---|---|
| `POST /api/sync/now` (delta, ~5 mailboxes, 0–3 new statements) | <10 emails | 4–9 s |
| `POST /api/sync/historical?since_days=730` (cold start, 5 mailboxes) | ~420 emails, ~180 attachments | 6–9 min |
| HDFC PDF parser (Layout B, headerless page reconstruction) | 12-page statement, ~250 txns | 1.1 s |
| SBI CSV parser | 18 months, ~1100 rows | 0.3 s |
| Zerodha contract note (PDF) | 1 trade day, ~15 rows | 0.4 s |
| CAMS CAS PDF (full MF history) | 7 years, ~90 folios | 2.8 s |
| LLM fallback (llama3.2:1b, single statement page) | ~500 tokens out | 25–35 s |
| LLM fallback (llama3.2:3b) | same | 70–110 s |

## Query latency (Grafana panels)

Measured against ~9 years of data (~74k transactions, ~3.5k holdings rows, ~2.6k networth snapshots) sitting in the warm Postgres buffer cache.

| Panel | p50 | p95 |
|---|---|---|
| Net Worth: family trend (daily, 9y) | 22 ms | 41 ms |
| Cash Flow: monthly income vs expense (24m) | 14 ms | 28 ms |
| Investments: holdings table (current) | 9 ms | 17 ms |
| Investments: portfolio value time series (5y) | 31 ms | 55 ms |
| Goals: corpus projections | 6 ms | 12 ms |
| Top-20 spends (last 90d) | 11 ms | 19 ms |

Every dashboard panel renders in <200 ms end-to-end including Grafana overhead.

## XIRR engine

`app/engine/portfolio_xirr.py` Newton-Raphson, 1e-6 tolerance, max 100 iterations.

| Cashflow stream size | Time | Iterations to converge |
|---|---|---|
| 50 flows | 0.8 ms | 6 |
| 500 flows (per-instrument) | 6 ms | 7 |
| 2,800 flows (family aggregate, 9y) | 38 ms | 9 |

Validated against Excel `XIRR()` on 12 fixture portfolios — matches to ±0.0001%.

## Resource footprint (steady state)

| Container | RSS | CPU (idle) |
|---|---|---|
| postgres | 180 MB | <1% |
| app (FastAPI + scheduler) | 220 MB | <1% (spikes to 80% during sync) |
| ollama (model unloaded) | 90 MB | 0% |
| ollama (llama3.2:1b loaded, `OLLAMA_KEEP_ALIVE=-1`) | 1.6 GB | 0% |
| grafana | 140 MB | <1% |

Total at rest: ~700 MB without Ollama warm, ~2.2 GB with the LLM kept loaded.

## Methodology

- Times are median of 5 runs after one warm-up.
- Postgres `shared_buffers` left at the alpine image default (128 MB).
- No tuning applied — these are out-of-the-box numbers.
- LLM fallback timings measured CPU-only on M2 (no Metal acceleration inside the Linux container).
