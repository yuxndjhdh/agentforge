# Changelog

## Unreleased

- Add reproducible A/B/C/D benchmark reporting and failure audit records;
  supplier billing rates remain `unavailable`.
- Normalize delivery document names and the package README path for
  case-sensitive clean clones.
- Validation: 112 tests passed and 1 platform-limited test was skipped;
  Docker live integration passed 3 tests. Complete quota/attack-matrix and
  remote CI evidence remain pending.

## 0.3.0 - 2026-09-10

- Persist benchmark jobs and run metadata across API service restarts.
- Add resumable API attempts with concurrent-resume conflict handling.
- Add runtime, LLM, tool, and verification tracing with optional OTLP export,
  trace IDs in API/event records, and Prometheus histograms.
- Add a pip-compile transitive lock, API E2E and packaging CI jobs, a 75%
  measured coverage floor, and a deterministic no-LLM demo script.
- Validation: 99 tests passed and 4 platform-limited integration tests were
  skipped locally; Docker live verification and real LLM benchmark experiments
  remain environment-dependent.

## 0.2.0 - 2026-09-10

- Correct standard pass@k and command-check semantics.
- Add independent durable runtime objects, SQLite metadata, JSONL events, checkpoints, cancellation, and recovery hooks.
- Add process-group execution limits and Docker/Podman container adapter.
- Add scoped/audited memory, 23-task benchmark reporting, FastAPI endpoints, metrics, packaging, and CI.
- Validation: 60 tests passed, 1 platform-limited test skipped, with 63% coverage; ruff, mypy, CLI selftest, compileall, and API smoke checks passed.

## 0.1.0

- Initial smolagents-based local code-agent MVP.
