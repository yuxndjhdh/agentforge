# External Benchmark Smoke Report

This is the P1-A pipeline smoke result generated from
`runs/external-benchmark/p1a-smoke-v3/report.json`. It is not an external
validity result: all three repositories are local deterministic fixtures and
the agent edits are supplied by a deterministic smoke agent.

## Evidence

- Scope: `pipeline_smoke`
- Dataset version: `external-v1-pipeline-smoke`
- Dataset SHA-256: `8d3eee0b38cf6824b14dbaa0217702659f914200f59334203a4e2bec4c72b607`
- Manifest SHA-256: `fef728fa840b0f3a3c64f9ab65faf4ec8c2d216220fc0931db4a10de4a5b58c3`
- Verifier audit: 3/3 valid; verifier bundles outside the agent workspace
- Base fail/reference pass: 3/3
- Pipeline episodes: 3/3 final pass
- First-attempt pass: 3/3
- Task-level bootstrap 95% CI: `[1.0, 1.0]` (descriptive only)
- Changed paths: each episode recorded `src/value.txt`; no disallowed path was modified

## Interpretation

The smoke confirms that pinned local repositories can be materialized, an
immutable verifier can distinguish the base and reference states, path policy
and trace/report plumbing are connected, and the summary preserves explicit
numerators and denominators. It does not measure model performance or external
generalization.

The formal P1 result still requires the frozen 30-task dataset, validation/
holdout repository isolation, two models, E0/E1/E2, five trials per task and
configuration, and manual audit evidence. No such result is claimed here.
