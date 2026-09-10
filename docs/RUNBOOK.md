# Runbook

## Local checks

```bash
python -m pytest tests/ -q
python -m compileall -q agentforge
python -m agentforge selftest --out runs/selftest
```

## Run with a model

Set `HARNESS_LLM_BASE`, `HARNESS_LLM_MODEL`, and `HARNESS_LLM_KEY`. For untrusted code use `HARNESS_SANDBOX_BACKEND=docker` or `podman`, verify the image is available, and leave `HARNESS_SANDBOX_NETWORK=0`.

```bash
python -m agentforge run path/to/repo "fix the failing test"
python -m agentforge verify fix-add --attempts 3
python -m agentforge trace runs/<run-id>/trace.json
```

## API

```bash
python -m agentforge serve --host 127.0.0.1 --port 8000
```

The service stores metadata in `AGENTFORGE_STATE_DB`, events and traces below `AGENTFORGE_TRACE_DIR`, and uses a worker pool for runs. Query `/runs/{id}` after submission; query `/runs/{id}/trace` for the complete event sequence.

## Recovery

If a worker exits, inspect the run status and latest complete checkpoint in SQLite. Call the runtime `resume(run_id, executor)` hook or resubmit through an API worker with `resume=True`. A successful run ID is idempotent and is never executed twice.

## Cleanup

Evaluation and verify temporary repositories are removed automatically unless `keep_workdir=True`. Generated `runs/` and `.agentforge/` directories are ignored by Git. Keep failed trace JSON and benchmark reports when investigating a regression.
