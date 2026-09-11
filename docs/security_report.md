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

The Windows development machine uses Docker Desktop's Linux engine. The
following live evidence was collected on 2026-09-10:

```text
Docker Server: 29.7.2
Kernel: 6.18.33.2-microsoft-standard-WSL2
Storage driver: overlayfs
Image: python:3.13-slim@sha256:9d2e5553305c7c7b0097999bb17187c69b921ccd6bc9d40e4bb5ebe652c00285
Integration tests: 3 passed
Disk quota status: requires-live-probe
```

`agentforge sandbox diagnose` reported the runtime and pinned image as
available. The integration tests verified non-root execution, a writable
`/workspace` mount under the read-only-root configuration, no
network/secret-environment behavior, timeout handling, and output
reclamation. They do not yet directly probe writes outside `/workspace` or
CPU/PID enforcement. The storage driver did not provide enough evidence for
the adapter to claim that `--storage-opt=size=` is enforced, so the quota
remains unverified and executions with a configured disk limit fail closed.
The default image reference is digest-pinned; user-supplied unpinned images
are rejected by the container executor.

## Verification On 2026-09-11

The current workspace repeated the live checks with Docker Desktop's Linux
engine:

```text
Docker Server: 29.7.2
Storage driver: overlayfs
Image: python:3.13-slim@sha256:9d2e5553305c7c7b0097999bb17187c69b921ccd6bc9d40e4bb5ebe652c00285
pytest -m integration tests/integration/test_container_sandbox.py -q: 5 passed
Disk quota status: disabled
```

The repeated integration result confirms the existing non-root, workspace,
network/secret-environment, timeout, output-reclamation, cgroup-limit, and
external-path write cases. It does not add Linux-runner evidence for the full
attack matrix or overlayfs quota enforcement. Those items remain open and this
report must not be used as complete R1 acceptance evidence.

The current Docker Desktop host also passed the following live probes through
the container executor:

```text
CPU cgroup:        cpu.max=100000 100000
Memory cgroup:     memory.max=536870912
PID cgroup:        pids.max=128
Container UID:     1000
/tmp write:        blocked
/etc write:        blocked
/agentforge write: blocked
/workspace write:  allowed
```

These values match the executor's configured 1 CPU, 512 MiB, and 128 PID
limits. The probes strengthen the evidence for this Docker Desktop daemon, but
they do not replace a Linux-runner matrix for alternate daemons, indirect
shell execution, fork-bomb handling, disk filling, or storage quota
enforcement.

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
