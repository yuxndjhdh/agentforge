# AgentForge v0.3.1 Release Record

Date: 2026-09-11

## Release

- Version: `v0.3.1`
- Tag target: `19d22773f1f84ca890fe2d5f51e5f3a6db807cee`
- Annotated tag object: `9f3dfdd8bf315879bdfb7fc26419b197b671e6ba`
- Final release commit: `19d22773f1f84ca890fe2d5f51e5f3a6db807cee`
- Candidate commit with green CI: `fae8d1b896feef3137043c40062a9274771b67ea`
- CI workflow: <https://github.com/yuxndjhdh/agentforge/actions/workflows/ci.yml>
- Candidate CI run: <https://github.com/yuxndjhdh/agentforge/actions/runs/34562905804>
- Candidate CI status: `Success`
- Tag-triggered CI run: <https://github.com/yuxndjhdh/agentforge/actions/runs/34563413730>
- Tag-triggered CI status: `Success`

The candidate commit is an ancestor of the final release commit. The final
release commit is both the `v0.3.1` tag target and the current
`origin/master` tip at validation time.

## Evidence

- Benchmark report: `docs/benchmark_report.md`
- Benchmark audit: `docs/benchmark_audit_20260911.md`
- Benchmark source commit: `e256238d158e36708019a7754876670744141e67`
- Security report: `docs/security_report.md`
- Tag contains benchmark, audit, and security reports: verified with
  `git cat-file -e v0.3.1:<path>` for each path.
- `HANDOFF.md` and the planning documents supplied during this validation are
  retained as current-worktree handoff context; they were created after the
  tag and are not retroactively claimed as contents of `v0.3.1`.
- Docker image: `python:3.13-slim@sha256:9d2e5553305c7c7b0097999bb17187c69b921ccd6bc9d40e4bb5ebe652c00285`
- Local clean clone: `agentforge-clean-8fbc944`, Python 3.13
- Workbench smoke: `python scripts/workbench_smoke.py` exercises the served
  page and the no-LLM submit/verify/trace/summary/export/Diff path.

## Known Limitations

- Benchmark supplier input/output rates were unavailable; cost fields remain
  `unavailable`, not zero or free.
- Docker Desktop live probes verified configured cgroup limits and read-only
  paths on this host, but overlayfs quota enforcement remains disabled or
  unverified.
- The complete Linux container attack matrix, including indirect shell
  execution, fork-bomb handling, disk filling, and alternate daemon behavior,
  remains outside the verified evidence.
- The benchmark has five trials per task and is descriptive rather than a
  statistical significance claim.
- This release has no 30-50 task external-repository holdout evaluation,
  second-model comparison, or 20-trial-per-fault injection matrix. Those are
  evidence-gathering follow-ups, not results claimed by this release.

## Local Validation After Tag

The following checks were run against the current worktree on 2026-09-11;
they are not retroactively attributed to the immutable tag target above:

- Full test suite: `118 passed, 1 skipped`, coverage `77.86%`.
- Docker integration: `5 passed`.
- Workbench smoke: passed without an external model.
- `ruff`, `mypy`, `compileall`, `pip check`, selftest, demo, wheel/sdist build,
  installed wheel CLI help, and `git diff --check`: passed.
- Docker diagnose: runtime available, digest-pinned image detected, overlayfs
  storage quota status `disabled`; auto backend correctly falls back local.
- Secret scan: no configured local `.env` value was found in source, reports,
  traces, logs, or other scanned artifacts. The ignored local `.env` itself
  contains a configured key-like value and was not included in the worktree
  changes; it must not be committed.
