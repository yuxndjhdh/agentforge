"""命令策略和进程执行边界。

本模块提供轻量的工具级策略校验，也提供不依赖 ``shell=True`` 的本地
进程执行器。真正的容器隔离由 :mod:`container_sandbox` 提供；本地执行器
会建立独立进程组，并在超时或输出超限时回收整个进程组。
"""

from __future__ import annotations

import os
import re
import shlex
import signal
import subprocess
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Sequence

SECRET_ENV = (
    "HARNESS_LLM_KEY",
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "API_KEY",
    "TOKEN",
    "SECRET",
    "PASSWORD",
    "KEY",
)

DEFAULT_DENY_COMMANDS = (
    "rm", "sudo", "curl", "wget", "nc", "ssh", "scp", "rsync", "git",
    "sh", "bash", "zsh", "fish", "pip", "apt", "yum", "dnf", "apk", "brew",
    "powershell", "pwsh", "cmd", "command.com",
)

DEFAULT_DENY_PATTERNS = (
    r"\brm\s+-(rf|fr|r)\b",
    r"\bcurl\b",
    r"\bwget\b",
    r"\bsudo\b",
    r"\b(nc|netcat)\b",
    r"\bsh\s+-c\b",
    r"\bbash\s+-c\b",
    r"\bzsh\s+-c\b",
    r"\bpip\s+install\b",
    r"\bgit\s+(push|reset\s+--hard|clean\b|checkout\s+-f)",
    r"\bcd\s+(/|~)",
    r"(?i)\b(invoke-webrequest|invoke-restmethod|start-process)\b",
    r"(?i)\b(certutil|bitsadmin)\b",
    r"(?i)-encodedcommand\b",
)

POLICY_VERSION = "2026-09-01"


@dataclass(frozen=True)
class PolicyDecision:
    allowed: bool
    action: str
    reason: str | None = None
    command: str = ""
    policy_version: str = POLICY_VERSION


@dataclass(frozen=True)
class ExecutionResult:
    returncode: int | None
    stdout: str
    stderr: str
    timed_out: bool = False
    output_limited: bool = False
    error: str | None = None


@dataclass
class Sandbox:
    """工具级策略和受控本地进程执行配置。

    ``whitelist`` 为空时采用默认拒绝策略，以保持开发环境可用；生产运行
    应配置白名单或使用容器执行器。
    """

    whitelist: tuple[str, ...] = ()
    deny_commands: tuple[str, ...] = DEFAULT_DENY_COMMANDS
    deny_patterns: tuple[str, ...] = DEFAULT_DENY_PATTERNS
    readonly: bool = False
    timeout: float = 30.0
    max_output_bytes: int = 256_000
    backend: str = "auto"  # auto | local | docker | podman
    network: bool = False
    cpu_limit: float = 1.0
    memory_limit_mb: int = 512
    pids_limit: int = 128
    disk_limit_mb: int = 1024
    image: str = "python:3.13-slim"
    policy_version: str = POLICY_VERSION
    decision_sink: Callable[[dict], None] | None = field(default=None, repr=False, compare=False)
    cancel_event: threading.Event | None = field(default=None, repr=False, compare=False)

    def check_command(self, command: str) -> str | None:
        """校验命令；被拒返回 ``Error: ...``，放行返回 ``None``。"""
        raw = str(command or "")
        if not raw.strip():
            return self._deny(raw, "empty command", "empty")
        try:
            argv = shlex.split(raw, posix=True)
        except ValueError as exc:
            return self._deny(raw, f"invalid command syntax: {exc}", "syntax")
        if not argv:
            return self._deny(raw, "empty command", "empty")

        first = argv[0]
        name = _executable_name(first)
        allowed_names = {_executable_name(x) for x in self.whitelist}
        if allowed_names and name not in allowed_names:
            return self._deny(
                raw,
                f"command '{first}' not in sandbox whitelist "
                f"(allowed: {', '.join(self.whitelist)})",
                "whitelist",
            )
        for denied in self.deny_commands:
            if name == _executable_name(denied):
                return self._deny(raw, f"command '{denied}' is denied by sandbox", "deny_command")
        for pattern in self.deny_patterns:
            try:
                matched = re.search(pattern, raw) is not None
            except re.error:
                matched = False
            if matched:
                return self._deny(raw, f"command {raw!r} matches sandbox deny pattern", "deny_pattern")

        # The executor uses argv directly. Reject control tokens instead of
        # silently changing the meaning of a command through a shell.
        if any(token in {";", "&&", "||", "|", ">", ">>", "<"} for token in argv):
            return self._deny(raw, "shell control operators are not allowed", "shell_operator")
        self._record(PolicyDecision(True, "command", command=raw))
        return None

    def check_write(self) -> str | None:
        """readonly 时全禁写；放行返回 ``None``。"""
        if self.readonly:
            message = "Error: sandbox is readonly (write/run blocked)"
            self._record(PolicyDecision(False, "write", message))
            return message
        self._record(PolicyDecision(True, "write"))
        return None

    def env(
        self,
        base: dict[str, str] | None = None,
        *,
        workdir: str | None = None,
    ) -> dict[str, str]:
        """复制环境并移除凭据及宿主容器控制变量。"""
        env = dict(os.environ if base is None else base)
        for name in list(env):
            upper = name.upper()
            if any(secret in upper for secret in SECRET_ENV) or upper in {
                "DOCKER_HOST",
                "DOCKER_CONTEXT",
                "KUBECONFIG",
                "GITHUB_TOKEN",
            }:
                del env[name]
        env["AGENTFORGE_SANDBOXED"] = "1"
        env["PYTHONNOUSERSITE"] = "1"
        if workdir:
            root = os.path.realpath(workdir)
            env["AGENTFORGE_WORKDIR"] = root
            env["HOME"] = root
            if os.name == "nt":
                env["USERPROFILE"] = root
        return env

    def execute(
        self,
        command: str | Sequence[str],
        *,
        cwd: str,
        env: dict[str, str] | None = None,
        timeout: float | None = None,
        max_output_bytes: int | None = None,
    ) -> ExecutionResult:
        """使用 argv + 独立进程组执行命令，并限制时间和输出。"""
        raw = command if isinstance(command, str) else " ".join(map(str, command))
        if isinstance(command, str):
            try:
                argv = shlex.split(command, posix=True)
            except ValueError as exc:
                return ExecutionResult(None, "", "", error=f"invalid command syntax: {exc}")
        else:
            argv = [str(part) for part in command]
        if not argv:
            return ExecutionResult(None, "", "", error="empty command")
        policy_error = self.check_command(raw)
        if policy_error:
            return ExecutionResult(None, "", policy_error, error=policy_error)
        if not os.path.isdir(cwd):
            return ExecutionResult(None, "", "", error="cwd is not a directory")

        limit = max(1024, int(max_output_bytes or self.max_output_bytes))
        if self.backend in {"auto", "docker", "podman"}:
            from .container_sandbox import ContainerExecutor

            container = ContainerExecutor(self)
            if container.available:
                return container.execute(
                    argv,
                    cwd=cwd,
                    env=env,
                    timeout=timeout,
                    max_output_bytes=limit,
                )
            if self.backend in {"docker", "podman"}:
                return ExecutionResult(None, "", "", error="container runtime is unavailable")
        try:
            proc = subprocess.Popen(
                argv,
                cwd=cwd,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=self.env(env, workdir=cwd),
                start_new_session=(os.name != "nt"),
                creationflags=(subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0),
            )
        except (OSError, ValueError) as exc:
            return ExecutionResult(None, "", "", error=f"{type(exc).__name__}: {exc}")

        stdout_buf: list[bytes] = []
        stderr_buf: list[bytes] = []
        output_limited = threading.Event()

        def read_stream(stream, target: list[bytes]) -> None:
            total = 0
            while True:
                chunk = stream.read(8192)
                if not chunk:
                    break
                remaining = limit - total
                if remaining > 0:
                    target.append(chunk[:remaining])
                    total += min(len(chunk), remaining)
                if len(chunk) > remaining or total >= limit:
                    output_limited.set()

        threads = [
            threading.Thread(target=read_stream, args=(proc.stdout, stdout_buf), daemon=True),
            threading.Thread(target=read_stream, args=(proc.stderr, stderr_buf), daemon=True),
        ]
        for thread in threads:
            thread.start()
        deadline = max(0.01, float(self.timeout if timeout is None else timeout))
        timed_out = False
        wait_deadline = time.monotonic() + deadline
        while proc.poll() is None:
            if self.cancel_event is not None and self.cancel_event.is_set():
                _terminate_process_tree(proc)
                break
            if output_limited.is_set():
                _terminate_process_tree(proc)
                break
            remaining = wait_deadline - time.monotonic()
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
        for thread in threads:
            thread.join(timeout=2.0)
        if proc.poll() is None:
            _terminate_process_tree(proc)
            try:
                proc.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                pass
        stdout = b"".join(stdout_buf).decode("utf-8", errors="replace")
        stderr = b"".join(stderr_buf).decode("utf-8", errors="replace")
        return ExecutionResult(
            proc.returncode,
            stdout,
            stderr,
            timed_out=timed_out,
            output_limited=output_limited.is_set(),
        )

    def _record(self, decision: PolicyDecision) -> None:
        if self.decision_sink is not None:
            payload = {
                "allowed": decision.allowed,
                "action": decision.action,
                "reason": decision.reason,
                "command": decision.command,
                "policy_version": self.policy_version,
            }
            try:
                self.decision_sink(payload)
            except Exception:
                # 记录失败不应改变工具行为。
                pass

    def _deny(self, command: str, reason: str, action: str) -> str:
        message = f"Error: {reason}"
        self._record(PolicyDecision(False, action, reason, command, self.policy_version))
        return message

    @classmethod
    def default(cls) -> "Sandbox":
        return cls()

    @classmethod
    def from_config(cls, cfg) -> "Sandbox":
        """从配置构建；用户 denylist 追加到默认策略。"""
        whitelist = _split(getattr(cfg, "sandbox_whitelist", ""))
        denylist = _split(getattr(cfg, "sandbox_denylist", ""))
        deny_patterns = _split(getattr(cfg, "sandbox_deny_patterns", ""))
        return cls(
            whitelist=whitelist,
            deny_commands=DEFAULT_DENY_COMMANDS + tuple(denylist),
            deny_patterns=DEFAULT_DENY_PATTERNS + tuple(deny_patterns),
            readonly=bool(getattr(cfg, "sandbox_readonly", False)),
            timeout=float(getattr(cfg, "sandbox_timeout", 30.0)),
            max_output_bytes=int(getattr(cfg, "sandbox_max_output_bytes", 256_000)),
            backend=str(getattr(cfg, "sandbox_backend", "auto")).lower(),
            network=bool(getattr(cfg, "sandbox_network", False)),
            cpu_limit=float(getattr(cfg, "sandbox_cpu_limit", 1.0)),
            memory_limit_mb=int(getattr(cfg, "sandbox_memory_limit_mb", 512)),
            pids_limit=int(getattr(cfg, "sandbox_pids_limit", 128)),
            disk_limit_mb=int(getattr(cfg, "sandbox_disk_limit_mb", 1024)),
            image=str(getattr(cfg, "sandbox_image", "python:3.13-slim")),
            policy_version=POLICY_VERSION,
        )


def _split(s: str) -> tuple[str, ...]:
    return tuple(x.strip() for x in str(s or "").split(",") if x.strip())


def _executable_name(value: str) -> str:
    """比较命令名时统一 Unix/Windows 路径和扩展名。"""
    name = str(value).strip().strip('"\'')
    name = name.replace("\\", "/").rsplit("/", 1)[-1].lower()
    if name.endswith(".exe"):
        name = name[:-4]
    return name


def _terminate_process_tree(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    if os.name == "nt":
        try:
            subprocess.run(
                ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
                timeout=2,
            )
        except (OSError, subprocess.TimeoutExpired):
            try:
                proc.kill()
            except OSError:
                pass
    else:
        killpg = getattr(os, "killpg", None)
        sigkill = getattr(signal, "SIGKILL", None)
        if killpg is None or sigkill is None:
            try:
                proc.kill()
            except OSError:
                pass
            return
        try:
            killpg(proc.pid, sigkill)
        except (ProcessLookupError, PermissionError):
            try:
                proc.kill()
            except OSError:
                pass
