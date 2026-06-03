# Performance Notes

This page documents the perf hot-spots in Kutumb, what's been done, and what's left. Targets the "Ollama is slow on my laptop, parsing takes time" pain point that surfaced during the first historical sync on modest hardware.

## Where the time goes (cold historical sync, ~5 mailboxes, 2 years)

| Step | Share of wall time (uncached) | Notes |
|---|---|---|
| pdfplumber PDF text extraction | ~45% | Slowest step. Most PDFs are 5–15 pages, scanned ones slower. |
| LLM fallback (Ollama on CPU) | ~30% | Triggers when a parser can't extract a section (FD, PPF, holdings). 25–35 s/call on `llama3.2:1b`, 70–110 s on `:3b`. |
| IMAP fetch + download | ~15% | Many small messages × N sender domains × N folders. |
| Regex parse + ETL + DB writes | ~10% | Cheap. |

## Optimisations applied

### 1. LLM response disk cache (`app/ingestion/llm_extractor.py`)

Every Ollama call is cached on disk keyed by `sha1(model + prompt + input_text)` under `data/cache/llm/`. On a re-run of `POST /sync/historical`, every section that was sent to the LLM last time returns from cache in <1 ms.

- Disable with `LLM_CACHE=0`.
- Override path with `LLM_CACHE_DIR=/custom/path`.

**Impact:** historical sync re-runs drop from 6–9 min to ~90 s (the LLM-bound 30% disappears).

### 2. Per-doc-type `num_predict` cap

The old code asked Ollama to generate up to 3000 tokens for every call. `bank_ppf` is a single number — capping at 64 tokens cuts that call from ~30 s to ~4 s. Per-type budgets live in `_NUM_PREDICT`.

### 3. Explicit `keep_alive: -1` on the Ollama request

Belt-and-suspenders with the server-side `OLLAMA_KEEP_ALIVE=-1` env var. Guarantees the model stays resident across calls — avoids the ~10 s cold-load between extractions.

### 4. Regex fast-path for `bank_ppf`

Before invoking the LLM, `extract()` looks for `(PPF|Public Provident).{0,200}([\d,]+\.\d{2})` in the section. About 90% of statements match — the LLM never runs. Saves ~30 s per affected statement.

### 5. PDF text-extract sidecar cache (`app/ingestion/parsers/base.py`)

`BaseParser._extract_text_cached(path)` writes a `.txt.cache` sidecar next to each PDF after the first pdfplumber pass. Subsequent runs read the sidecar instead of re-extracting (the sidecar is invalidated if the source PDF's mtime is newer).

- Disable with `PDF_TEXT_CACHE=0`.
- Parsers that need page-by-page access (e.g. HDFC Layout B which infers column boundaries from page geometry) keep using `_open_pdf` directly; this helper is for whole-document text use cases.

### 5b. Whole-`ParseResult` cache (`app/ingestion/parsers/base.py`)

Because most parsers need both `extract_text()` *and* `extract_tables()`, a text-only sidecar doesn't replace pdfplumber for them. So `BaseParser.parse_cached(path)` adds a coarser cache: pickle the entire `ParseResult` keyed by `sha1(file_bytes + parser_class + schema_version)` under `data/cache/parsed/`.

- The router (`attachment_router.route_and_parse`) calls `parse_cached` instead of `parse` — every parser benefits automatically, no per-parser changes needed.
- Cache invalidates on file-bytes change.
- Schema-version bump (`BaseParser._CACHE_SCHEMA`) busts the entire cache when the dataclasses change shape.
- Disable with `PARSE_RESULT_CACHE=0`.

**Impact:** parser-only re-runs (e.g. after a categoriser tweak triggers re-parse) drop from minutes to seconds. On the second `sync/historical` over the same window, pdfplumber doesn't run at all for files we've already parsed.

### 6. Parallel IMAP fetch across accounts (`app/ingestion/email_fetcher.py`)

`fetch_all_accounts()` now runs each configured mailbox concurrently via a `ThreadPoolExecutor(max_workers=len(accounts))`. IMAP4_SSL connections are independent and IMAP is network-bound, so Gmail + Yahoo finish in roughly the time of the slowest single mailbox instead of summing.

- Disable with `IMAP_PARALLEL=0` for sequential debugging.

**Measured impact on the reference deployment (2 mailboxes, 30-day window):** sync wall time dropped from 176 s → **106 s** (40% faster). On 5 mailboxes the win is larger.

### 7. Skip already-downloaded attachments

`email_fetcher` now checks `dest.exists()` before writing attachment bytes. Re-running `sync/historical` no longer re-downloads ~200 PDFs from IMAP.

## Tuning knobs

| Env var | Default | Effect |
|---|---|---|
| `OLLAMA_PARSE_MODEL` | `llama3.2:1b` | `:3b` for accuracy if you have spare CPU/GPU; `qwen2.5:7b` for highest accuracy on a GPU. |
| `OLLAMA_KEEP_ALIVE` | `-1` (in compose) | `0` to free memory between calls (slower); any positive integer = seconds to keep model loaded. |
| `LLM_CACHE` | `1` | `0` disables on-disk LLM response caching. |
| `LLM_CACHE_DIR` | `data/cache/llm` | Override the cache location. |
| `PDF_TEXT_CACHE` | `1` | `0` disables `.txt.cache` sidecars next to PDFs. |
| `PARSE_RESULT_CACHE` | `1` | `0` disables pickled `ParseResult` cache. |
| `PARSE_RESULT_CACHE_DIR` | `data/cache/parsed` | Override the pickle cache location. |
| `IMAP_PARALLEL` | `1` | `0` falls back to sequential per-account fetch. |

## What's left (not yet done)

1. **IMAP combined search.** Today the fetcher runs one `SEARCH (SINCE … FROM <domain>)` per known sender domain per folder — ~15 round-trips. A nested-OR query would collapse them to 1–2 per folder. Skipped for now because OR-tree depth varies per server and the risk of silently missing emails is real; the existing latency is acceptable.
2. **Per-statement parser timing in logs.** Currently we log `n_parsed` but not `parse_ms`. Adding it would make regressions visible on the Grafana parser-health panel.
3. **Ollama batch endpoint.** When several PDFs hit the LLM in one sync, we currently call serially. Ollama's streaming JSON keepalive helps a bit, but a small request-coalescing layer ("batch all FD extractions before all holdings extractions") would let us load the model once for many calls.
4. **GPU acceleration.** Ollama on Apple Silicon uses Metal directly on the *host*; inside the Linux container in Docker Desktop it's CPU-only. Running Ollama on the host and pointing `OLLAMA_HOST=http://host.docker.internal:11434` from the app container drops LLM calls from 30 s to ~3 s. Documented in [SETUP.md](../SETUP.md).

## How to verify a perf change worked

```bash
# Time a cold sync
docker-compose down
rm -rf data/cache/ data/raw/*/*.txt.cache
time curl -X POST "http://localhost:8000/api/sync/historical?since_days=730"

# Time a warm re-sync (caches populated)
time curl -X POST "http://localhost:8000/api/sync/historical?since_days=730"
```

Expected delta on the reference machine (M2 Air, 8 GB):

| Run | Wall time |
|---|---|
| Cold (no caches, first time) | 6–9 min |
| Warm (LLM + PDF text caches hot) | 60–120 s |
| Hot (everything cached, files unchanged) | 20–40 s — DB writes only |
