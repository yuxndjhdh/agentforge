# Threat Model

## Assets

- Host credentials, source code outside the assigned repository, Docker/Podman sockets, and user files.
- Repository integrity and benchmark correctness.
- Trace, state, memory, and API credentials.

## Adversaries

- A model output that intentionally requests destructive, network, credential, or parent-directory access.
- Repository code that spawns children, creates symlinks or hard links, forks, loops, or emits unbounded output.
- A stale or malicious durable memory entry.
- A client that submits a repository path outside the service's intended scope.

## Controls

- All file-tool and evaluator paths are resolved against a real workdir. Absolute paths, traversal, symlink components, and hard-linked file reads/writes are rejected.
- Commands are parsed into argv and checked against whitelist, deny command, deny pattern, and shell-control rules. No tool command uses `shell=True`.
- Local processes have a new process group, scrubbed environment, timeout, bounded stdout/stderr, and child-tree termination. This is a containment improvement, not a kernel security boundary.
- Docker/Podman mode is non-root, no-network by default, capability-dropped, read-only root, workdir-only mount, and resource-limited. Explicit container mode fails if the runtime is unavailable.
- Memory is scoped by project/user, versioned, auditable, length-limited, JSON-encoded when injected, and instruction-kind entries are excluded from automatic system-context injection.
- API run paths are validated as directories. Deployments should additionally configure an allowlisted root, authentication, and a separate worker host.

## Residual risk

`HARNESS_SANDBOX_BACKEND=auto` falls back to local execution when no container runtime is installed so development remains usable. Do not process untrusted repositories in that mode. Docker escape, kernel vulnerabilities, malicious base images, and host-level service compromise remain outside this repository and require operational controls.
