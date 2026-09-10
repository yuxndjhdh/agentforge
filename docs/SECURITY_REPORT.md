# AgentForge Security Verification Report

Date: 2026-09-10

## Scope

This report covers the command policy, path boundary checks, local process
limits, and the Docker/Podman adapter. Local execution is a development
fallback and is not treated as a kernel security boundary.

## Evidence Available In This Repository

- `tests/test_sandbox.py` covers traversal, symlink/hard-link rejection,
  command policy, environment scrubbing, timeout, output limits, and explicit
  container fail-closed behavior.
- `tests/integration/test_container_sandbox.py` covers non-root execution,
  workspace-only writes, no-network/secret environment behavior, timeout, and
  output reclamation when a live daemon is available.
- `agentforge sandbox diagnose` reports runtime availability, image digest
  pinning, storage driver, and whether the disk quota capability has been
  verified.
- The default image is `python:3.13-slim@sha256:9d2e5553305c7c7b0097999bb17187c69b921ccd6bc9d40e4bb5ebe652c00285`.

## Current Machine

The Windows development machine used for this change has no Docker or Podman
executable available. Therefore the live container tests were not claimed as
passed, and no storage-driver capability was fabricated. The diagnostic
command reports this as unavailable and explicit container mode fails closed.
The default image reference is digest-pinned; user-supplied unpinned images
are rejected by the container executor.

Run the following on Linux CI or a host with a configured daemon:

```powershell
.venv\Scripts\python -m agentforge sandbox diagnose
.venv\Scripts\python -m pytest -m integration tests/integration/test_container_sandbox.py -q
```

## Residual Risks

- `HARNESS_SANDBOX_BACKEND=auto` can fall back to local execution. Do not use
  that mode for untrusted repositories.
- A digest-pinned image and a verified storage quota are deployment
  requirements. The adapter refuses to claim a disk quota when the daemon
  capability is unknown.
- Docker/Podman daemon, kernel, image provenance, and host policy remain
  operational trust boundaries outside this repository.
