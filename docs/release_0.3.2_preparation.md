# AgentForge v0.3.2 Release Preparation

Date: 2026-09-11

## Status

`v0.3.2` was released from `master`: release commit
`f0681f50bdebe7213dfffebb90c8c689f19394ea` (`release: prepare v0.3.2`) was
tagged `v0.3.2`, pushed to `origin`, and the package version is now `0.3.2`.

The prior `v0.3.1` tag remains immutable and points to
`19d22773f1f84ca890fe2d5f51e5f3a6db807cee`.

## Remote Evidence

- Workflow: <https://github.com/yuxndjhdh/agentforge/actions/workflows/ci.yml>
- Current master CI run:
  <https://github.com/yuxndjhdh/agentforge/actions/runs/34578760405>
- Current master CI status: `Success`
- v0.3.2 tag CI run:
  <https://github.com/yuxndjhdh/agentforge/actions/runs/34583164675> —
  `Success` for SHA `f0681f50bdebe7213dfffebb90c8c689f19394ea` (`test` on
  3.11/3.12/3.13, `docker-sandbox`, `api-e2e`, `packaging`)
- Release commit CI run on `master`:
  <https://github.com/yuxndjhdh/agentforge/actions/runs/34583159435> —
  `Success`
- Benchmark source commit: `e256238d158e36708019a7754876670744141e67`

## Included Candidate Work

- Run Workbench submit, verify, Diff, Attempts, Tool Calls, Trace, metrics,
  cancel, resume, and export flow.
- Resume responses and trace events identify the checkpoint sequence used.
- `scripts/workbench_smoke.py` provides a no-model page/API acceptance path.
- `scripts/summarize_impact.py` generates `docs/impact_summary.json` from the
  four dated benchmark reports.

The generated Impact summary validates against 460 episodes and the benchmark
source commit above, and is included in the `v0.3.2` release commit.

## Local Validation

- Full test suite: `118 passed, 1 skipped`, coverage `77.78%`.
- Docker integration: `5 passed`.
- Workbench smoke: passed without an external model.
- `ruff`, `mypy`, `compileall`, `pip check`, CLI help, selftest, demo, and
  wheel/sdist build: passed.
- Docker runtime is available with the digest-pinned image; overlayfs quota is
  `disabled`, so the existing fail-closed behavior remains in effect.
- `git diff --check`: passed.

## Evidence Boundaries

This preparation does not claim a 30-50 task external-repository holdout, a
second-model comparison, a 20-trial fault-injection matrix, developer trial
Impact data, or complete Linux/Podman container attack evidence. Benchmark
cost remains `unavailable` without supplier rates. The ignored local `.env`
configuration is not part of the release artifacts.

## Release Actions Completed (2026-09-11)

1. The generated Impact summary and this preparation record were included in
   the release commit. Handoff and planning documents stay untracked and are
   listed in `.gitignore`.
2. The package version was bumped to `0.3.2`; release commit `f0681f5` and tag
   `v0.3.2` were created and pushed to `origin`.
3. Tag-triggered CI was verified: run
   <https://github.com/yuxndjhdh/agentforge/actions/runs/34583164675>
   completed `Success` for SHA `f0681f5`.

The external-task, fault-injection, security, and developer-trial goals under
*Evidence Boundaries* remain open; this release does not claim them.
