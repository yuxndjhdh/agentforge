# AgentForge v0.3.1 Release Record

Date: 2026-09-11

## Release

- Version: `v0.3.1`
- Tag target: the release commit tagged as `v0.3.1`
- Candidate commit with green CI: `fae8d1b896feef3137043c40062a9274771b67ea`
- CI workflow: <https://github.com/yuxndjhdh/agentforge/actions/workflows/ci.yml>
- Candidate CI run: <https://github.com/yuxndjhdh/agentforge/actions/runs/34562905804>

The final tag target and its tag-triggered CI run are resolved after the
release commit is created and pushed. This record intentionally does not infer
that tag SHA before Git creates it.

## Evidence

- Benchmark report: `docs/benchmark_report.md`
- Benchmark audit: `docs/benchmark_audit_20260911.md`
- Benchmark source commit: `e256238d158e36708019a7754876670744141e67`
- Security report: `docs/security_report.md`
- Docker image: `python:3.13-slim@sha256:9d2e5553305c7c7b0097999bb17187c69b921ccd6bc9d40e4bb5ebe652c00285`
- Local clean clone: `agentforge-clean-8fbc944`, Python 3.13

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
