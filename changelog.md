# Changelog

## 0.3.2 - 2026-09-11

- Add the run Workbench with per-run model and sandbox selection, independent
  verification, bounded workspace Diff, Attempts, Tool Calls, Trace, metrics,
  cancel, resume, and JSON export views.
- Persist verification feedback and expose the checkpoint sequence used by a
  resumed attempt.
- Add a reproducible no-model Workbench smoke script and a machine-readable
  Impact summary generated from the four benchmark reports. Impact values
  remain descriptive single-model benchmark evidence; cost remains
  `unavailable` without supplier rates.

## 0.3.1 - 2026-09-11

- Make benchmark cost reporting fail closed when positive supplier rates are
  unavailable; token totals remain available without a billing conclusion.
- Add reproducible A/B/C/D benchmark audit evidence and live Docker probes for
  cgroup CPU/memory/PID limits and read-only paths outside `/workspace`.
- Stabilize Linux CI imports and cross-platform mypy checks; verify Python
  3.11/3.12/3.13, API E2E, Docker integration, packaging, and coverage in
  GitHub Actions.
- Validation: 114 local tests passed and 1 platform-limited test was skipped;
  local Docker integration passed 5 tests. Benchmark supplier rates,
  overlayfs quota enforcement, and the full container attack matrix remain
  explicitly unverified.

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
