"""Optional Docker/Podman execution backend.

The backend is deliberately a small adapter: the policy remains in
``Sandbox`` and the mounted workspace is the only writable host path. When
the backend is ``auto`` and no container runtime exists, callers receive a
local process execution result from ``Sandbox``; explicit ``docker`` or
``podman`` configuration fails closed.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Sequence

from .sandbox import ExecutionResult, Sandbox, _terminate_process_tree


@dataclass(frozen=True)
class ContainerRuntimeDiagnostic:
    requested: str
    runtime: str | None
    available: bool
    version: str | None
    storage_driver: str | None
    storage_limit_status: str
    image: str
    image_pinned: bool
    error: str | None = None

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


class ContainerExecutor:
    def __init__(self, sandbox: Sandbox):
        self.sandbox = sandbox
        self.runtime = self._find_runtime()

    @property
    def available(self) -> bool:
        return self.runtime is not None

    def diagnose(self) -> ContainerRuntimeDiagnostic:
        if self.runtime is None:
            return ContainerRuntimeDiagnostic(
                requested=self.sandbox.backend,
                runtime=None,
                available=False,
                version=None,
                storage_driver=None,
                storage_limit_status="unavailable",
                image=self.sandbox.image,
                image_pinned=_is_pinned_image(self.sandbox.image),
                error="container runtime unavailable: docker/podman executable was not found",
            )
        version = _probe(self.runtime, ["version", "--format", "{{.Server.Version}}"])
        if version is None and Path(self.runtime).name.lower().startswith("podman"):
            version = _probe(self.runtime, ["version", "--format", "{{.Version.Version}}"])
        driver = _probe(self.runtime, ["info", "--format", "{{.Driver}}"])
        if driver is None and Path(self.runtime).name.lower().startswith("podman"):
            driver = _probe(self.runtime, ["info", "--format", "{{.store.graphDriverName}}"])
        storage_status = _storage_limit_status(driver, self.sandbox.disk_limit_mb)
        error = None if version else "container runtime did not return a server version"
        return ContainerRuntimeDiagnostic(
            requested=self.sandbox.backend,
            runtime=self.runtime,
            available=version is not None,
            version=version,
            storage_driver=driver,
            storage_limit_status=storage_status,
            image=self.sandbox.image,
            image_pinned=_is_pinned_image(self.sandbox.image),
            error=error,
        )

    def _find_runtime(self) -> str | None:
        requested = self.sandbox.backend
        if requested == "docker":
            return shutil.which("docker")
        if requested == "podman":
            return shutil.which("podman")
        if requested == "auto":
            return shutil.which("docker") or shutil.which("podman")
        return None

    def command(self, argv: Sequence[str], cwd: str, env: dict[str, str] | None = None) -> list[str]:
        if not self.runtime:
            raise RuntimeError("container runtime is unavailable")
        root = str(Path(cwd).resolve())
        cmd = [
            self.runtime,
            "run",
            "--rm",
            "--init",
            "--cap-drop=ALL",
            "--security-opt=no-new-privileges",
            "--network=bridge" if self.sandbox.network else "--network=none",
            "--user=1000:1000",
            f"--cpus={max(0.1, self.sandbox.cpu_limit):g}",
            f"--memory={max(16, self.sandbox.memory_limit_mb)}m",
            f"--pids-limit={max(16, self.sandbox.pids_limit)}",
            "--read-only",
            "-v",
            f"{root}:/workspace:rw",
            "-w",
            "/workspace",
        ]
        # The container gets no inherited host environment. Pass only stable
        # non-secret values needed by common test runners.
        safe_env = {"AGENTFORGE_SANDBOXED": "1", "AGENTFORGE_WORKDIR": "/workspace"}
        if env:
            for name, value in env.items():
                upper = name.upper()
                if upper in {"LANG", "LC_ALL", "LC_CTYPE", "CI"} or upper.startswith("PYTHON"):
                    safe_env[name] = value
        for name, value in safe_env.items():
            cmd.extend(["-e", f"{name}={value}"])
        if self.sandbox.disk_limit_mb > 0:
            # The option is intentionally explicit. Unsupported drivers fail
            # closed in execute() instead of silently claiming a quota.
            cmd.append(f"--storage-opt=size={max(64, self.sandbox.disk_limit_mb)}m")
        cmd.extend([self.sandbox.image, *map(str, argv)])
        return cmd

    def execute(
        self,
        argv: Sequence[str],
        *,
        cwd: str,
        env: dict[str, str] | None = None,
        timeout: float | None = None,
        max_output_bytes: int = 256_000,
    ) -> ExecutionResult:
        diagnostic = self.diagnose()
        if not diagnostic.available:
            return ExecutionResult(None, "", "", error=diagnostic.error or "container runtime unavailable")
        if not diagnostic.image_pinned:
            return ExecutionResult(
                None,
                "",
                "",
                error="sandbox image must be pinned by a sha256 digest",
            )
        if self.sandbox.disk_limit_mb > 0 and diagnostic.storage_limit_status != "supported":
            return ExecutionResult(
                None,
                "",
                "",
                error=(
                    "disk quota cannot be verified for container storage driver "
                    f"{diagnostic.storage_driver or 'unknown'}; refusing to run"
                ),
            )
        try:
            command = self.command(argv, cwd, env)
            proc = subprocess.Popen(
                command,
                cwd=cwd,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=(os.name != "nt"),
                creationflags=(subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0),
            )
        except (OSError, ValueError, RuntimeError) as exc:
            return ExecutionResult(None, "", "", error=f"{type(exc).__name__}: {exc}")

        output_limited = threading.Event()
        stdout_buf: list[bytes] = []
        stderr_buf: list[bytes] = []

        def read_stream(stream, target: list[bytes]) -> None:
            total = 0
            while True:
                chunk = stream.read(8192)
                if not chunk:
                    break
                remaining = max_output_bytes - total
                if remaining > 0:
                    target.append(chunk[:remaining])
                    total += min(len(chunk), remaining)
                if len(chunk) > remaining or total >= max_output_bytes:
                    output_limited.set()

        readers = [
            threading.Thread(target=read_stream, args=(proc.stdout, stdout_buf), daemon=True),
            threading.Thread(target=read_stream, args=(proc.stderr, stderr_buf), daemon=True),
        ]
        for reader in readers:
            reader.start()
        timed_out = False
        deadline = time.monotonic() + max(0.01, float(timeout or self.sandbox.timeout))
        while proc.poll() is None:
            if self.sandbox.cancel_event is not None and self.sandbox.cancel_event.is_set():
                _terminate_process_tree(proc)
                break
            if output_limited.is_set():
                _terminate_process_tree(proc)
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_out = True
                _terminate_process_tree(proc)
                break
            try:
                proc.wait(timeout=min(0.1, remaining))
            except subprocess.TimeoutExpired:
                continue
        if output_limited.is_set() and proc.poll() is None:
            _terminate_process_tree(proc)
        for reader in readers:
            reader.join(timeout=2)
        if proc.poll() is None:
            _terminate_process_tree(proc)
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                pass
        return ExecutionResult(
            proc.returncode,
            b"".join(stdout_buf).decode("utf-8", errors="replace"),
            b"".join(stderr_buf).decode("utf-8", errors="replace"),
            timed_out=timed_out,
            output_limited=output_limited.is_set(),
        )


def _probe(runtime: str, args: list[str]) -> str | None:
    try:
        result = subprocess.run(
            [runtime, *args],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    value = result.stdout.strip()
    return value or None


def _is_pinned_image(image: str) -> bool:
    return "@sha256:" in str(image).lower()


def _storage_limit_status(driver: str | None, disk_limit_mb: int) -> str:
    if disk_limit_mb <= 0:
        return "disabled"
    if not driver:
        return "unknown"
    # Docker's size storage-opt requires a filesystem and daemon-specific
    # quota support. Do not label common drivers supported without probing the
    # actual daemon configuration; an explicit integration run must verify it.
    return "requires-live-probe"
