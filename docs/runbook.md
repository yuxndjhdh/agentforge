# Runbook

## Local checks

```bash
python -m pytest tests/ -q
python -m compileall -q agentforge
python -m agentforge selftest --out runs/selftest
python scripts/demo_selftest.py --out runs/demo
```

## Run with a model

Set `HARNESS_LLM_BASE`, `HARNESS_LLM_MODEL`, and `HARNESS_LLM_KEY`. For untrusted code use `HARNESS_SANDBOX_BACKEND=docker` or `podman`, verify the image is available, and leave `HARNESS_SANDBOX_NETWORK=0`.

```bash
python -m agentforge run path/to/repo "fix the failing test"
python -m agentforge verify fix-add --attempts 3
python -m agentforge trace runs/<run-id>/trace.json
```

For benchmark ablations, use the explicit compression and Verify flags and keep
the four output directories separate. The runner writes the variant metadata
into each `report.json`; `scripts/summarize_benchmark.py` generates the
comparison Markdown without manually copying numbers.

## API

```bash
python -m agentforge serve --host 127.0.0.1 --port 8000
```

The service stores run and benchmark metadata in `AGENTFORGE_STATE_DB`, events and traces below `AGENTFORGE_TRACE_DIR`, and uses a worker pool for runs. Query `/runs/{id}` after submission; query `/runs/{id}/trace` for the complete event sequence. Benchmark job configuration, task names, report paths, and failures remain queryable after a service restart.

## Recovery

If a worker exits, inspect the run status and latest complete checkpoint in SQLite. Call `POST /runs/{id}/resume` for a failed or cancelled run; the response contains the new attempt ID while the original run ID and attempt history are retained. A successful run ID is idempotent and is never executed twice. A resume request already in progress returns HTTP 409.

## Cleanup

Evaluation and verify temporary repositories are removed automatically unless `keep_workdir=True`. Generated `runs/` and `.agentforge/` directories are ignored by Git. Keep failed trace JSON and benchmark reports when investigating a regression.
