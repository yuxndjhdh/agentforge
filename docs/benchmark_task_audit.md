# Benchmark Task Audit

Date: 2026-09-10

The canonical `BENCHMARK_TASKS` registry contains 23 versioned tasks. The
task order is stable and both CLI and API use the same selector. A request for
an unknown task is rejected instead of silently running a smaller benchmark.

## Coverage

- 23 total tasks, including 15 multi-file tasks.
- 18 easy and 5 medium tasks.
- Tags cover bug fixing, tests, API changes, configuration migration, CLI,
  security/path handling, refactoring, state, edge cases, documentation, and
  type contracts.
- Every task has a checked-in seed builder, source label, task version, and
  resource limits.
- Command checks execute with `shell=False`, require exit code zero, and can
  assert the complete stdout line sequence. This avoids treating a matching
  digit inside unrelated output as success.

## Runner evidence

`tests/test_benchmark.py` uses a fake solver path to verify pass@1, pass@3,
pass@5, p50/p95 inputs, token and cost aggregation, task order, redacted
configuration, atomic report writing, and rebuilding over a corrupt report.
The report stores each episode and trace path; failures are not removed from
the episode list.

## Real experiment status

The four requested A/B/C/D experiments have not been claimed as executed on
this machine because `HARNESS_LLM_KEY` is not configured. A real run must use
`--trials 5 --k 5`, retain all traces, and record the AgentForge commit,
model, temperature, tokenizer, and configuration snapshot before generating
`docs/benchmark_report.md`.
