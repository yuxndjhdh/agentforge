# AgentForge v0.3.2 Release Preparation

Date: 2026-09-11

## Status

The latest Workbench and Impact changes are on `master` at
`0c2e10a5d3e412cce52f16db5ace574510a8a3f7`, which is equal to
`origin/master`. This is a release preparation record only: no `v0.3.2`
release commit or tag has been created from this worktree.

The package version remains `0.3.1` until a release commit is explicitly
authorized. The existing `v0.3.1` tag remains immutable and points to
`19d22773f1f84ca890fe2d5f51e5f3a6db807cee`.

## Remote Evidence

- Workflow: <https://github.com/yuxndjhdh/agentforge/actions/workflows/ci.yml>
- Current master CI run:
  <https://github.com/yuxndjhdh/agentforge/actions/runs/34578760405>
- Current master CI status: `Success`
- Benchmark source commit: `e256238d158e36708019a7754876670744141e67`

## Included Candidate Work

- Run Workbench submit, verify, Diff, Attempts, Tool Calls, Trace, metrics,
  cancel, resume, and export flow.
- Resume responses and trace events identify the checkpoint sequence used.
- `scripts/workbench_smoke.py` provides a no-model page/API acceptance path.
- `scripts/summarize_impact.py` generates `docs/impact_summary.json` from the
  four dated benchmark reports.

The generated Impact summary currently exists in the worktree and validates
against 460 episodes and the benchmark source commit above. It is not claimed
as part of `v0.3.1` until it is included in a future release commit.

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

## Release Actions Still Required

1. Decide whether the generated Impact summary and preparation record belong
   in the release commit; keep handoff and planning documents separate unless
   intentionally promoted to project documentation.
2. With explicit release authorization, bump the package version, create the
   release commit and `v0.3.2` tag, and push them.
3. Verify tag-triggered CI, record its URL and SHA, and then reassess whether
   the external-task, fault-injection, security, and developer-trial goals are
   complete.
