# AgentForge Benchmark Audit

Date: 2026-09-11

## Source And Integrity

- Frozen commit: `e256238d158e36708019a7754876670744141e67`
- Model: `deepseek-v4-flash-vision-exp`
- Source directories: `runs/benchmarks/v0.3.0-A-20260911`, `B-20260911`, `C-20260911`, `D-20260911`
- Each variant contains 23 tasks x 5 trials = 115 episodes.
- The four variants contain 460 accessible `trace.json` files in total.
- Reports agree on commit, model, task order, seed, sandbox image, network, resource limits, tokenizer, and context budget. Only compression and Verify settings differ.
- Failed episodes and their traces remain in the source directories.

The comparison report was regenerated from the dated source reports with:

```powershell
.venv\Scripts\python.exe scripts\summarize_benchmark.py `
  runs\benchmarks\v0.3.0-A-20260911\report.json `
  runs\benchmarks\v0.3.0-B-20260911\report.json `
  runs\benchmarks\v0.3.0-C-20260911\report.json `
  runs\benchmarks\v0.3.0-D-20260911\report.json `
  --out docs\benchmark_report.md
```

## Failure Samples

The following samples were selected from the independent episode records. All listed trace paths exist.

| Category | Variant/task/trial | Trace | Audit result |
| --- | --- | --- | --- |
| `state_mismatch` | A / `fix-clamp` / 0 | `runs/benchmarks/v0.3.0-A-20260911/fix-clamp/20260911-081425-727/trace.json` | Independent check feedback required `min(hi, max(lo, value))`; reward remained 0. |
| `state_mismatch` | A / `fix-clamp` / 1 | `runs/benchmarks/v0.3.0-A-20260911/fix-clamp/20260911-081435-448/trace.json` | Final file state did not satisfy the declared check; reward remained 0. |
| `state_mismatch` | A / `fix-clamp` / 3 | `runs/benchmarks/v0.3.0-A-20260911/fix-clamp/20260911-081458-740/trace.json` | Final file state did not satisfy the declared check; reward remained 0. |
| `tool_error` | A / `fix-clamp` / 2 | `runs/benchmarks/v0.3.0-A-20260911/fix-clamp/20260911-081449-716/trace.json` | Trace contains a tool error and the independent check still failed. |
| `tool_error` | A / `fix-retry` / 1 | `runs/benchmarks/v0.3.0-A-20260911/fix-retry/20260911-081942-906/trace.json` | Trace contains a tool error; the declared retry implementation check failed. |
| `tool_error` | A / `security-path` / 1 | `runs/benchmarks/v0.3.0-A-20260911/security-path/20260911-082914-915/trace.json` | Trace contains a tool error; the declared path-boundary check failed. |

The reward is produced by `agentforge.eval.eval_reward()` and the task's
`file_contains`, `command`, or `file_absent` checks. It is not taken from the
agent's final answer. Verify feedback is generated from the same independent
checks, and C/D records show first success rate 0.800, final success rate
1.000, and average attempt count 1.2.

## Sensitive Data And Cost

- Report configuration redacts the API key as `<redacted>`.
- A scan of source, reports, traces, logs, and documentation found no configured key value.
- The reports have zero cost rates from the run environment. The generated report therefore marks cost and Verify incremental cost as `unavailable`; token totals remain usable, but no billing conclusion is made.
- Five trials per task provide descriptive measurements only, not a significance test.

## Remaining External Evidence

- Docker Desktop integration passed, but Linux live quota enforcement and the full R1 attack matrix still require a Linux Docker runner.
- Remote CI URL and green matrix jobs require remote repository execution evidence.
- A release commit/tag requires the external evidence above and explicit release authorization.
