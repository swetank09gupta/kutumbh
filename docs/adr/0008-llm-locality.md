# ADR-0008: LLM runs locally via Ollama

**Status:** Accepted
**Date:** 2024-04

## Context

Some statements need fuzzy extraction (new bank PDF layouts, OCR-quality scans). An LLM is the natural tool. Where does it run?

## Options

- **OpenAI / Anthropic / Gemini API** — best quality, lowest latency, *highest* privacy risk. Statements contain PAN, salary, account numbers.
- **Self-hosted on GPU** — best of both, but assumes hardware most household users don't have.
- **Local Ollama on CPU** — slower (25–35s per call for `llama3.2:1b`) but private and runs on a laptop.

## Decision

Ollama running as a sidecar container. Default model `llama3.2:1b` for CPU; `OLLAMA_PARSE_MODEL` env var lets users opt into `llama3.2` (3b) or `qwen2.5:7b` if they have a GPU.

`OLLAMA_KEEP_ALIVE=-1` keeps the model resident so we don't pay cold-start (~10s) on every call.

## Consequences

- ✅ No statement data ever leaves the host. This is the entire point of the project.
- ✅ Free at the margin once the model is pulled.
- ❌ Slower than hosted APIs. Acceptable because the LLM is the fallback path (see ADR-0009), not the hot path.
- ❌ Resident model footprint ~1.6 GB RSS. The user can downsize or unload by setting `OLLAMA_KEEP_ALIVE=0` if they're memory-constrained.
