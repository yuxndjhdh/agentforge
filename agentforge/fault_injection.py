"""Independent-process fault-injection protocol for AgentForge runtime state.

The runner is intentionally deterministic and conservative.  It provides a
small, reviewable controller/worker harness for smoke and pilot runs; it does
not claim that a smoke run is the formal 460-trial reliability matrix.
"""

from __future__ import annotations

import argparse
import ctypes
import errno
import hashlib
import json
import os
import platform
import signal
import sqlite3
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from statistics import median
from typing import Any, Iterable

from .runtime import AgentRuntime, RunStatus, RuntimeStore
from .sandbox import _terminate_process_tree
from .trace import RunTrace

FAULT_SCHEMA_VERSION = 1
FAULT_SUITE_VERSION = "fault-v2"
FAULT_PROTOCOL_VERSION = "p2-formal-460-v2"


@dataclass(frozen=True)
class FaultScenario:
    name: str
    trigger_event: str
    action: str
    expected: str
    recoverable: bool
    required_events: tuple[str, ...] = ()


FAULT_SCENARIOS: tuple[FaultScenario, ...] = (
    FaultScenario("F01_llm_before_request", "llm.request.started", "terminate-worker", "recover", True),
    FaultScenario("F02_llm_after_response", "llm.response.received", "terminate-worker", "recover", True),
    FaultScenario("F03_tool_before_start", "tool_call.before_start", "terminate-worker", "recover", True),
    FaultScenario("F04_tool_completed_persisted", "tool_call.completed", "terminate-worker", "recover", True),
    FaultScenario("F05_effect_before_completion", "fault.side_effect.applied", "terminate-worker", "fail-closed", False),
    FaultScenario("F06_checkpoint_completed", "checkpoint.created", "terminate-worker", "recover", True),
    FaultScenario("F07_jsonl_partial_tail", "events.ready", "corrupt-jsonl-tail", "recover", True),
    FaultScenario("F08_checkpoint_corruption", "checkpoint.latest", "corrupt-checkpoint", "recover", True),
    FaultScenario("F09_checkpoint_schema", "checkpoint.latest", "corrupt-schema", "fail-closed", False),
    FaultScenario("F10_sqlite_temporarily_unwritable", "sqlite.write.before", "inject-storage-error", "recover", True),
    FaultScenario("F11_api_process_restart", "api.request.started", "restart-worker", "recover", True),
    FaultScenario("F12_docker_unavailable", "docker.probe", "force-container-unavailable", "fail-closed", False),
    FaultScenario("F13_run_timeout", "run.timeout", "terminate-worker", "timeout", False),
    FaultScenario("F14_user_cancel", "run.cancel", "cancel", "cancelled", False),
)

SCENARIO_BY_NAME = {scenario.name: scenario for scenario in FAULT_SCENARIOS}
FAULT_SCOPE_TRIALS: dict[str, int] = {"smoke": 1, "pilot": 5}
FAULT_FORMAL_TRIALS: dict[str, int] = {
    "F01_llm_before_request": 20,
    "F02_llm_after_response": 20,
    "F03_tool_before_start": 20,
    "F04_tool_completed_persisted": 50,
    "F05_effect_before_completion": 50,
    "F06_checkpoint_completed": 50,
    "F07_jsonl_partial_tail": 20,
    "F08_checkpoint_corruption": 20,
    "F09_checkpoint_schema": 20,
    "F10_sqlite_temporarily_unwritable": 20,
    "F11_api_process_restart": 50,
    "F12_docker_unavailable": 20,
    "F13_run_timeout": 50,
    "F14_user_cancel": 50,
}
FAULT_FORMAL_DECLARED_TOTAL = 460
FAULT_FORMAL_MATRIX_TOTAL = sum(FAULT_FORMAL_TRIALS.values())
if FAULT_FORMAL_MATRIX_TOTAL != FAULT_FORMAL_DECLARED_TOTAL:
    raise RuntimeError("formal fault matrix counts do not match the declared protocol total")
RESULT_KEYS = (
    "resume_attempted",
    "resume_succeeded",
    "verification_passed",
    "duplicate_side_effects",
    "orphan_processes",
    "trace_complete",
    "state_consistent",
    "recovery_latency_seconds",
    "false_success",
    "resume_checkpoint_sequence",
)


def fault_matrix_plan(scope: str, *, seed: int = 0) -> list[dict[str, Any]]:
    """Return the immutable trial identity list for a named fault scope."""

    normalized = scope.strip().lower()
    if normalized in FAULT_SCOPE_TRIALS:
        counts = {scenario.name: FAULT_SCOPE_TRIALS[normalized] for scenario in FAULT_SCENARIOS}
    elif normalized == "formal":
        counts = dict(FAULT_FORMAL_TRIALS)
    else:
        raise ValueError("fault scope must be smoke, pilot, or formal")
    if set(counts) != set(SCENARIO_BY_NAME):
        raise ValueError("fault formal matrix does not cover the registered scenarios")
    return [
        {
            "scenario": scenario.name,
            "trial": trial,
            "seed": seed + trial,
            "expected": scenario.expected,
            "trigger_event": scenario.trigger_event,
            "action": scenario.action,
        }
        for scenario in FAULT_SCENARIOS
        for trial in range(counts[scenario.name])
    ]


def fault_matrix_sha256(plan: list[dict[str, Any]]) -> str:
    payload = json.dumps(plan, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _json_write(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def _json_read(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _git_commit() -> str | None:
    root = Path(__file__).resolve().parents[1]
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(root),
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except OSError:
        return None
    return result.stdout.strip() if result.returncode == 0 else None


def _tree_hash(root: Path) -> str:
    digest = hashlib.sha256()
    if not root.is_dir():
        return digest.hexdigest()
    for path in sorted(root.rglob("*")):
        if path.is_symlink() or not path.is_file():
            continue
        relative = path.relative_to(root).as_posix()
        if any(part in {".git", "__pycache__"} for part in Path(relative).parts):
            continue
        digest.update(relative.encode("utf-8"))
        digest.update(b"\x00")
        digest.update(hashlib.sha256(path.read_bytes()).digest())
        digest.update(b"\x00")
    return digest.hexdigest()


@dataclass(frozen=True)
class FaultTrial:
    schema_version: int
    suite_version: str
    scenario: str
    trial: int
    seed: int
    git_commit: str | None
    platform: str
    python: str
    injection: dict[str, Any]
    expected: str
    result: dict[str, Any]
    artifacts: dict[str, Any]

    @classmethod
    def create(
        cls,
        scenario: FaultScenario,
        *,
        trial: int,
        seed: int,
        injection: dict[str, Any],
        result: dict[str, Any],
        artifacts: dict[str, Any],
    ) -> "FaultTrial":
        return cls(
            schema_version=FAULT_SCHEMA_VERSION,
            suite_version=FAULT_SUITE_VERSION,
            scenario=scenario.name,
            trial=trial,
            seed=seed,
            git_commit=_git_commit(),
            platform=platform.platform(),
            python=sys.version,
            injection=dict(injection),
            expected=scenario.expected,
            result={"resume_checkpoint_sequence": None, **dict(result)},
            artifacts=dict(artifacts),
        )

    def validate(self) -> None:
        if self.schema_version != FAULT_SCHEMA_VERSION:
            raise ValueError(f"unsupported fault trial schema version: {self.schema_version}")
        if self.suite_version != FAULT_SUITE_VERSION:
            raise ValueError(f"unsupported fault suite version: {self.suite_version}")
        if self.scenario not in SCENARIO_BY_NAME:
            raise ValueError(f"unknown fault scenario: {self.scenario}")
        if self.trial < 0 or self.seed < 0:
            raise ValueError("fault trial and seed must be non-negative")
        for key in ("trigger_event", "action", "observed"):
            if key not in self.injection:
                raise ValueError(f"fault trial injection is missing {key}")
        for key in RESULT_KEYS:
            if key not in self.result:
                raise ValueError(f"fault trial result is missing {key}")
        if not isinstance(self.artifacts, dict):
            raise ValueError("fault trial artifacts must be an object")

    def to_dict(self) -> dict[str, Any]:
        self.validate()
        return {
            "schema_version": self.schema_version,
            "suite_version": self.suite_version,
            "scenario": self.scenario,
            "trial": self.trial,
            "seed": self.seed,
            "git_commit": self.git_commit,
            "platform": self.platform,
            "python": self.python,
            "injection": dict(self.injection),
            "expected": self.expected,
            "result": dict(self.result),
            "artifacts": dict(self.artifacts),
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "FaultTrial":
        if not isinstance(raw, dict):
            raise ValueError("fault trial must be an object")
        trial = cls(
            schema_version=int(raw.get("schema_version", -1)),
            suite_version=str(raw.get("suite_version", "")),
            scenario=str(raw.get("scenario", "")),
            trial=int(raw.get("trial", -1)),
            seed=int(raw.get("seed", -1)),
            git_commit=raw.get("git_commit"),
            platform=str(raw.get("platform", "")),
            python=str(raw.get("python", "")),
            injection=dict(raw.get("injection", {})),
            expected=str(raw.get("expected", "")),
            result={"resume_checkpoint_sequence": None, **dict(raw.get("result", {}))},
            artifacts=dict(raw.get("artifacts", {})),
        )
        trial.validate()
        return trial


class SideEffectLedger:
    """Append-only effect evidence used to distinguish replay from duplication."""

    def __init__(self, path: str | Path):
        self.path = Path(path)

    def append(self, key: str, payload: Any, *, effect_count: int = 1) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        record = {
            "semantic_key": key,
            "invocation_count": 1,
            "payload_sha256": hashlib.sha256(
                json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8")
            ).hexdigest(),
            "effect_count": effect_count,
            "timestamp": time.time(),
            "pid": os.getpid(),
        }
        with self.path.open("a", encoding="utf-8", newline="\n") as stream:
            stream.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")
            stream.flush()
            os.fsync(stream.fileno())

    def records(self) -> list[dict[str, Any]]:
        if not self.path.is_file():
            return []
        result: list[dict[str, Any]] = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                result.append(value)
        return result


def _barrier(trial_root: Path, trigger_event: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    barrier = trial_root / "barrier.json"
    control = trial_root / "control.json"
    _json_write(
        barrier,
        {"trigger_event": trigger_event, "payload": payload or {}, "pid": os.getpid(), "timestamp": time.time()},
    )
    deadline = time.monotonic() + 60
    while not control.exists():
        if time.monotonic() > deadline:
            raise TimeoutError(f"controller did not release barrier: {trigger_event}")
        time.sleep(0.01)
    try:
        value = _json_read(control)
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _emit_and_barrier(runtime: AgentRuntime, ctx: Any, trial_root: Path, event: str, *, resume: bool, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    runtime.emit(ctx.run.id, event, attempt_id=ctx.attempt.id, payload=payload or {})
    if resume:
        return {}
    return _barrier(trial_root, event, payload)


def _spawn_child_tree(trial_root: Path) -> subprocess.Popen:
    pid_file = trial_root / "pids.json"
    command = [sys.executable, "-m", "agentforge.fault_injection", "--sleep-child", str(pid_file)]
    child = subprocess.Popen(command, cwd=str(Path(__file__).resolve().parents[1]))
    _json_write(trial_root / "worker_pid.json", {"pid": os.getpid()})
    return child


def _sleep_child_main(pid_file: str) -> int:
    path = Path(pid_file)
    grandchild = subprocess.Popen(
        [sys.executable, "-m", "agentforge.fault_injection", "--sleep-leaf"],
        cwd=str(Path(__file__).resolve().parents[1]),
    )
    _json_write(path, {"child": os.getpid(), "grandchild": grandchild.pid})
    try:
        while True:
            time.sleep(1)
    finally:
        _terminate_process_tree(grandchild)


def _sleep_leaf_main() -> int:
    while True:
        time.sleep(1)


def _worker_executor(scenario: FaultScenario, trial_root: Path, *, resume: bool):
    ledger = SideEffectLedger(trial_root / "side_effects.jsonl")
    ledger.path.parent.mkdir(parents=True, exist_ok=True)
    ledger.path.touch(exist_ok=True)
    child: subprocess.Popen | None = None

    def execute(ctx: Any) -> str:
        nonlocal child
        if scenario.name in {"F13_run_timeout", "F14_user_cancel"}:
            child = _spawn_child_tree(trial_root)
        if scenario.name == "F01_llm_before_request":
            _emit_and_barrier(ctx.runtime, ctx, trial_root, "llm.request.started", resume=resume)
            ctx.step("llm", number=1, output="deterministic-response")
        elif scenario.name == "F02_llm_after_response":
            _emit_and_barrier(ctx.runtime, ctx, trial_root, "llm.response.received", resume=resume)
            ctx.step("llm", number=1, output="deterministic-response")
        elif scenario.name == "F03_tool_before_start":
            _emit_and_barrier(ctx.runtime, ctx, trial_root, "tool_call.before_start", resume=resume)
            ctx.call_tool("fixed-write", lambda: "written", idempotency_key="fixed-write")
        elif scenario.name == "F04_tool_completed_persisted":
            def idempotent_effect() -> str:
                ledger.append("idempotent-write", {"value": "ok"})
                return "ok"

            ctx.call_tool("idempotent-write", idempotent_effect, idempotency_key="idempotent-write")
            _emit_and_barrier(ctx.runtime, ctx, trial_root, "tool_call.completed", resume=resume)
        elif scenario.name == "F05_effect_before_completion":
            def non_idempotent_effect() -> str:
                ledger.append("non-idempotent-publish", {"value": "published"})
                ctx.runtime.emit(ctx.run.id, "fault.side_effect.applied", attempt_id=ctx.attempt.id, payload={"key": "non-idempotent-publish"})
                if not resume:
                    _barrier(trial_root, "fault.side_effect.applied", {"key": "non-idempotent-publish"})
                return "published"

            ctx.call_tool("non-idempotent-publish", non_idempotent_effect, idempotency_key="non-idempotent-publish")
        elif scenario.name == "F06_checkpoint_completed":
            ctx.checkpoint({"cursor": 1, "trace_steps": ctx.trace_steps}, complete=True)
            _emit_and_barrier(ctx.runtime, ctx, trial_root, "checkpoint.created", resume=resume, payload={"sequence": 1})
            ctx.step("action", number=1, output="after-checkpoint")
        elif scenario.name == "F07_jsonl_partial_tail":
            _emit_and_barrier(ctx.runtime, ctx, trial_root, "events.ready", resume=resume)
            ctx.step("action", number=1, output="event-tail")
        elif scenario.name in {"F08_checkpoint_corruption", "F09_checkpoint_schema"}:
            ctx.checkpoint({"cursor": 1, "trace_steps": ctx.trace_steps}, complete=True)
            ctx.checkpoint({"cursor": 2, "trace_steps": ctx.trace_steps}, complete=True)
            action = _emit_and_barrier(ctx.runtime, ctx, trial_root, "checkpoint.latest", resume=resume, payload={"sequence": 2})
            if action.get("action") in {"corrupt-checkpoint", "corrupt-schema"} and not resume:
                raise RuntimeError("fault injected into latest checkpoint")
            ctx.step("action", number=2, output="checkpoint-recovered")
        elif scenario.name == "F10_sqlite_temporarily_unwritable":
            action = _emit_and_barrier(ctx.runtime, ctx, trial_root, "sqlite.write.before", resume=resume)
            if action.get("action") == "inject-storage-error" and not resume:
                raise sqlite3.OperationalError("injected temporary SQLite write failure")
            ctx.step("action", number=1, output="storage-retried")
        elif scenario.name == "F11_api_process_restart":
            _emit_and_barrier(ctx.runtime, ctx, trial_root, "api.request.started", resume=resume)
            ctx.step("api", number=1, output="restarted")
        elif scenario.name == "F12_docker_unavailable":
            action = _emit_and_barrier(ctx.runtime, ctx, trial_root, "docker.probe", resume=resume)
            if action.get("action") == "force-container-unavailable" and not resume:
                ctx.runtime.emit(ctx.run.id, "runtime.fail_closed", attempt_id=ctx.attempt.id, payload={"reason": "container runtime unavailable"})
                raise RuntimeError("container runtime unavailable; fail-closed")
            ctx.step("container", number=1, output="not-run")
        elif scenario.name == "F13_run_timeout":
            _emit_and_barrier(ctx.runtime, ctx, trial_root, "run.timeout", resume=resume)
            while True:
                ctx.check_cancelled()
                time.sleep(0.05)
        elif scenario.name == "F14_user_cancel":
            _emit_and_barrier(ctx.runtime, ctx, trial_root, "run.cancel", resume=resume)
            while True:
                ctx.check_cancelled()
                time.sleep(0.05)
        else:
            raise ValueError(f"unsupported worker scenario: {scenario.name}")
        return "deterministic worker complete"

    execute.child = lambda: child  # type: ignore[attr-defined]
    return execute


def _worker_main(scenario_name: str, trial_root_value: str, run_id: str, *, resume: bool) -> int:
    scenario = SCENARIO_BY_NAME[scenario_name]
    trial_root = Path(trial_root_value).resolve()
    workspace = trial_root / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "state.txt").write_text("base\n", encoding="utf-8")
    store = RuntimeStore(trial_root / "state.sqlite3", trial_root / "events")
    runtime = AgentRuntime(store, trace_root=trial_root / "traces")
    executor = _worker_executor(scenario, trial_root, resume=resume)
    result: Any = None
    error: str | None = None
    try:
        if resume:
            store.recover_stale_runs()
            result = runtime.resume(run_id, executor)
        else:
            result = runtime.run(
                f"fault smoke: {scenario.name}",
                str(workspace),
                executor,
                run_id=run_id,
                max_duration=0.25 if scenario.name == "F13_run_timeout" else None,
            )
    except BaseException as exc:
        error = f"{type(exc).__name__}: {exc}"
    finally:
        child = getattr(executor, "child", lambda: None)()
        if child is not None and child.poll() is None:
            _terminate_process_tree(child)
        if result is not None:
            _json_write(
                trial_root / "worker_result.json",
                {
                    "status": result.run.status.value,
                    "answer": result.answer,
                    "error": result.run.error,
                    "trace": str(result.trace_path),
                },
            )
        elif error is not None:
            _json_write(trial_root / "worker_error.json", {"error": error})
        store.close()
    return 0 if error is None else 1


def _write_control(trial_root: Path, action: str, **payload: Any) -> None:
    _json_write(trial_root / "control.json", {"action": action, **payload})


def _wait_for_barrier(trial_root: Path, timeout: float) -> dict[str, Any] | None:
    barrier = trial_root / "barrier.json"
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if barrier.is_file():
            try:
                value = _json_read(barrier)
            except (OSError, json.JSONDecodeError):
                value = None
            if isinstance(value, dict) and value.get("trigger_event"):
                return value
        time.sleep(0.01)
    return None


def _wait_process(process: subprocess.Popen, timeout: float) -> bool:
    try:
        process.wait(timeout=timeout)
        return True
    except subprocess.TimeoutExpired:
        return False


def _start_worker(trial_root: Path, scenario: FaultScenario, run_id: str, *, resume: bool = False) -> tuple[subprocess.Popen, Any]:
    log = (trial_root / ("worker-resume.log" if resume else "worker.log")).open("w", encoding="utf-8")
    command = [
        sys.executable,
        "-m",
        "agentforge.fault_injection",
        "--worker",
        "--scenario",
        scenario.name,
        "--trial-root",
        str(trial_root),
        "--run-id",
        run_id,
    ]
    if resume:
        command.append("--resume")
    creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) if os.name == "nt" else 0
    process = subprocess.Popen(
        command,
        cwd=str(Path(__file__).resolve().parents[1]),
        stdin=subprocess.DEVNULL,
        stdout=log,
        stderr=subprocess.STDOUT,
        start_new_session=(os.name != "nt"),
        creationflags=creationflags,
    )
    return process, log


def _events(trial_root: Path, run_id: str) -> list[dict[str, Any]]:
    store = RuntimeStore(trial_root / "state.sqlite3", trial_root / "events")
    try:
        return store.events.read(run_id)
    finally:
        store.close()


_CHILD_SUBREAPER_ENABLED = False


def _enable_child_subreaper() -> bool:
    """Make Linux orphaned worker descendants reparent to this controller."""

    global _CHILD_SUBREAPER_ENABLED
    if _CHILD_SUBREAPER_ENABLED:
        return True
    if sys.platform != "linux":
        return False
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        prctl = libc.prctl
        prctl.argtypes = [
            ctypes.c_int,
            ctypes.c_ulong,
            ctypes.c_ulong,
            ctypes.c_ulong,
            ctypes.c_ulong,
        ]
        prctl.restype = ctypes.c_int
        if prctl(36, 1, 0, 0, 0) != 0:  # PR_SET_CHILD_SUBREAPER
            return False
    except (AttributeError, OSError, TypeError):
        return False
    _CHILD_SUBREAPER_ENABLED = True
    return True


def _linux_process_state(pid: int) -> str | None:
    if sys.platform != "linux":
        return None
    try:
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="ascii")
    except (OSError, UnicodeError):
        return None
    _, separator, remainder = stat.rpartition(")")
    if not separator:
        return None
    fields = remainder.split()
    return fields[0] if fields else None


def _pid_exists(pid: int) -> bool:
    if pid <= 0:
        return False
    if _linux_process_state(pid) == "Z":
        return False
    try:
        os.kill(pid, 0)
    except (OSError, ProcessLookupError):
        return False
    return True


def _reap_process_ids(pids: Iterable[int], *, timeout: float = 2.0) -> None:
    """Reap descendant PIDs adopted by this controller on POSIX systems."""

    if os.name == "nt" or not hasattr(os, "waitpid"):
        return
    pending = list(dict.fromkeys(int(pid) for pid in pids if int(pid) > 0))
    wait_nohang = getattr(os, "WNOHANG", 1)
    deadline = time.monotonic() + timeout
    while pending and time.monotonic() < deadline:
        remaining: list[int] = []
        for pid in pending:
            try:
                waited, _ = os.waitpid(pid, wait_nohang)
            except ChildProcessError:
                continue
            except OSError as exc:
                if exc.errno in {errno.ECHILD, errno.ESRCH}:
                    continue
                remaining.append(pid)
                continue
            if waited == 0:
                remaining.append(pid)
        if not remaining:
            return
        pending = remaining
        time.sleep(0.02)


def _terminate_process_ids(pids: Iterable[int]) -> None:
    """Reclaim known descendants even when their worker parent already exited."""

    unique = list(dict.fromkeys(int(pid) for pid in pids if int(pid) > 0))
    for pid in reversed(unique):
        if not _pid_exists(pid):
            continue
        if os.name == "nt":
            try:
                subprocess.run(
                    ["taskkill", "/PID", str(pid), "/T", "/F"],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    check=False,
                    timeout=2,
                )
            except (OSError, subprocess.TimeoutExpired):
                pass
        else:
            try:
                os.kill(pid, getattr(signal, "SIGKILL", signal.SIGTERM))
            except (OSError, ProcessLookupError):
                pass
    _reap_process_ids(unique)
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline and any(_pid_exists(pid) for pid in unique):
        time.sleep(0.02)
    _reap_process_ids(unique)


def _corrupt_checkpoint(trial_root: Path, *, schema: bool) -> None:
    store = RuntimeStore(trial_root / "state.sqlite3", trial_root / "events")
    try:
        row = store._connection.execute(
            "SELECT run_id, sequence FROM checkpoints WHERE is_event = 0 ORDER BY sequence DESC LIMIT 1"
        ).fetchone()
        if row is None:
            raise RuntimeError("no checkpoint to corrupt")
        if schema:
            store._connection.execute(
                "UPDATE checkpoints SET schema_version = 999 WHERE run_id = ? AND sequence = ?",
                (row["run_id"], row["sequence"]),
            )
        else:
            store._connection.execute(
                "UPDATE checkpoints SET state_json = ? WHERE run_id = ? AND sequence = ?",
                ("{corrupt", row["run_id"], row["sequence"]),
            )
    finally:
        store.close()


def _mark_timeout(trial_root: Path, run_id: str) -> None:
    store = RuntimeStore(trial_root / "state.sqlite3", trial_root / "events")
    try:
        store.update_run(run_id, status=RunStatus.FAILED, error="run exceeded max duration")
        store.append_event(run_id, "run.failed", payload={"error": "run exceeded max duration"})
        events = store.events.read(run_id)
        RunTrace(
            run_id=run_id,
            workdir=str(trial_root / "workspace"),
            task="fault smoke: F13_run_timeout",
            status=RunStatus.FAILED.value,
            events=events,
        ).dump(trial_root / "traces" / run_id / "trace.json")
    finally:
        store.close()


def _trial_result(
    scenario: FaultScenario,
    *,
    trial_root: Path,
    run_id: str,
    injection_observed: bool,
    resume_attempted: bool,
    recovery_started: float | None,
    process_pids: list[int],
) -> FaultTrial:
    worker_result: dict[str, Any] = {}
    result_path = trial_root / "worker_result.json"
    if result_path.is_file():
        try:
            value = _json_read(result_path)
            if isinstance(value, dict):
                worker_result = value
        except (OSError, json.JSONDecodeError):
            pass
    events = _events(trial_root, run_id) if (trial_root / "state.sqlite3").is_file() else []
    event_types = {str(event.get("type")) for event in events}
    ledger = SideEffectLedger(trial_root / "side_effects.jsonl")
    ledger_records = ledger.records()
    duplicate_effects = max(0, sum(int(item.get("effect_count", 0)) for item in ledger_records) - 1) if ledger_records else 0
    status = str(worker_result.get("status", ""))
    if not status and (trial_root / "state.sqlite3").is_file():
        store = RuntimeStore(trial_root / "state.sqlite3", trial_root / "events")
        try:
            run = store.get_run(run_id)
            status = run.status.value if run is not None else ""
        finally:
            store.close()
    if scenario.name == "F13_run_timeout" and not status:
        status = RunStatus.FAILED.value
    verification = (
        status == RunStatus.SUCCEEDED.value
        if scenario.expected == "recover"
        else status == RunStatus.CANCELLED.value
        if scenario.expected == "cancelled"
        else status == RunStatus.FAILED.value
    )
    if scenario.name in {"F07_jsonl_partial_tail", "F08_checkpoint_corruption"}:
        verification = verification and any(event == "run.completed" for event in event_types)
    if scenario.name == "F09_checkpoint_schema":
        verification = verification and not any(event == "run.completed" for event in event_types)
    if scenario.name == "F12_docker_unavailable":
        verification = verification and "runtime.fail_closed" in event_types
    orphan = sum(_pid_exists(pid) for pid in process_pids)
    trace_path = worker_result.get("trace")
    if not trace_path:
        fallback_trace = trial_root / "traces" / run_id / "trace.json"
        if fallback_trace.is_file():
            trace_path = str(fallback_trace)
    required_events = scenario.required_events or (scenario.trigger_event,)
    trace_complete = (
        bool(trace_path and Path(trace_path).is_file())
        and bool(events)
        and all(event in event_types for event in required_events)
    )
    state_consistent = status in {item.value for item in RunStatus} or scenario.expected == "timeout"
    false_success = scenario.expected in {"fail-closed", "timeout"} and status == RunStatus.SUCCEEDED.value
    recovery_latency = time.monotonic() - recovery_started if recovery_started is not None else 0.0
    resume_checkpoint_sequence = None
    for event in events:
        if event.get("type") != "run.started":
            continue
        payload = event.get("payload")
        if isinstance(payload, dict) and payload.get("resume"):
            resume_checkpoint_sequence = payload.get("checkpoint_sequence")
    result = {
        "resume_attempted": resume_attempted,
        "resume_succeeded": bool(resume_attempted and status == RunStatus.SUCCEEDED.value),
        "verification_passed": bool(verification),
        "duplicate_side_effects": duplicate_effects,
        "orphan_processes": orphan,
        "trace_complete": trace_complete,
        "state_consistent": state_consistent,
        "recovery_latency_seconds": recovery_latency,
        "false_success": false_success,
        "resume_checkpoint_sequence": resume_checkpoint_sequence,
    }
    artifacts = {
        "trace": str(Path(trace_path).relative_to(trial_root)) if trace_path and Path(trace_path).is_relative_to(trial_root) else None,
        "events": "events/{run_id}/events.jsonl".format(run_id=run_id),
        "database": "state.sqlite3",
        "workspace_hash": _tree_hash(trial_root / "workspace"),
        "side_effect_ledger": "side_effects.jsonl",
    }
    return FaultTrial.create(
        scenario,
        trial=0,
        seed=0,
        injection={
            "trigger_event": scenario.trigger_event,
            "action": scenario.action,
            "observed": injection_observed,
        },
        result=result,
        artifacts=artifacts,
    )


def run_fault_trial(
    scenario: FaultScenario | str,
    *,
    trial: int = 0,
    seed: int = 0,
    out_dir: str | Path = "runs/fault-injection/v1",
    timeout_seconds: float = 20.0,
) -> FaultTrial:
    """Run one controller/worker trial and always write ``trial.json``."""

    selected = SCENARIO_BY_NAME[scenario] if isinstance(scenario, str) else scenario
    _enable_child_subreaper()
    trial_root = Path(out_dir).resolve() / selected.name / str(trial)
    trial_root.mkdir(parents=True, exist_ok=True)
    run_id = f"fault-{selected.name}-{trial}-{seed}"
    process, log = _start_worker(trial_root, selected, run_id)
    observed_barrier = _wait_for_barrier(trial_root, timeout_seconds)
    recovery_started: float | None = None
    resume_attempted = False
    process_pids: list[int] = []
    pid_path = trial_root / "pids.json"
    pid_deadline = time.monotonic() + 1.0
    while not pid_path.is_file() and time.monotonic() < pid_deadline:
        time.sleep(0.01)
    if pid_path.is_file():
        try:
            pids = _json_read(pid_path)
            process_pids = [int(pids.get("child", 0)), int(pids.get("grandchild", 0))]
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            process_pids = []
    if process.poll() is None and observed_barrier is not None:
        if selected.action in {"terminate-worker", "restart-worker"}:
            _terminate_process_tree(process)
            process.wait(timeout=5)
            if selected.expected in {"recover", "fail-closed"}:
                resume_attempted = True
                recovery_started = time.monotonic()
                resume_process, resume_log = _start_worker(trial_root, selected, run_id, resume=True)
                _wait_process(resume_process, timeout_seconds)
                resume_log.close()
                if resume_process.poll() is None:
                    _terminate_process_tree(resume_process)
        elif selected.action == "corrupt-jsonl-tail":
            _write_control(trial_root, selected.action)
            process.wait(timeout=timeout_seconds)
            event_path = trial_root / "events" / run_id / "events.jsonl"
            with event_path.open("a", encoding="utf-8") as stream:
                stream.write('{"partial":')
            resume_attempted = True
            recovery_started = time.monotonic()
            resume_process, resume_log = _start_worker(trial_root, selected, run_id, resume=True)
            _wait_process(resume_process, timeout_seconds)
            resume_log.close()
        elif selected.action in {"corrupt-checkpoint", "corrupt-schema"}:
            _corrupt_checkpoint(trial_root, schema=selected.action == "corrupt-schema")
            _write_control(trial_root, selected.action)
            process.wait(timeout=timeout_seconds)
            if selected.action == "corrupt-checkpoint":
                resume_attempted = True
                recovery_started = time.monotonic()
                resume_process, resume_log = _start_worker(trial_root, selected, run_id, resume=True)
                _wait_process(resume_process, timeout_seconds)
                resume_log.close()
        elif selected.action == "inject-storage-error":
            _write_control(trial_root, selected.action)
            process.wait(timeout=timeout_seconds)
            resume_attempted = True
            recovery_started = time.monotonic()
            resume_process, resume_log = _start_worker(trial_root, selected, run_id, resume=True)
            _wait_process(resume_process, timeout_seconds)
            resume_log.close()
        elif selected.action == "force-container-unavailable":
            _write_control(trial_root, selected.action)
            process.wait(timeout=timeout_seconds)
        elif selected.action == "cancel":
            store = RuntimeStore(trial_root / "state.sqlite3", trial_root / "events")
            try:
                store.update_run(run_id, cancel_requested=True)
            finally:
                store.close()
            _write_control(trial_root, selected.action)
            _wait_process(process, timeout_seconds)
            if process.poll() is None:
                _terminate_process_tree(process)
        else:
            _write_control(trial_root, selected.action)
            _wait_process(process, timeout_seconds)
    elif process.poll() is None:
        _terminate_process_tree(process)
    if selected.name == "F13_run_timeout":
        _mark_timeout(trial_root, run_id)
    if process.poll() is None:
        _terminate_process_tree(process)
    if process.poll() is not None:
        process.wait(timeout=5)
    _terminate_process_ids(process_pids)
    log.close()
    trial_record = _trial_result(
        selected,
        trial_root=trial_root,
        run_id=run_id,
        injection_observed=observed_barrier is not None,
        resume_attempted=resume_attempted,
        recovery_started=recovery_started,
        process_pids=process_pids,
    )
    trial_record = FaultTrial(
        schema_version=trial_record.schema_version,
        suite_version=trial_record.suite_version,
        scenario=trial_record.scenario,
        trial=trial,
        seed=seed,
        git_commit=trial_record.git_commit,
        platform=trial_record.platform,
        python=trial_record.python,
        injection=trial_record.injection,
        expected=trial_record.expected,
        result=trial_record.result,
        artifacts=trial_record.artifacts,
    )
    _json_write(trial_root / "trial.json", trial_record.to_dict())
    return trial_record


def _rate(numerator: int, denominator: int) -> dict[str, Any]:
    return {"numerator": numerator, "denominator": denominator, "rate": numerator / denominator if denominator else None}


def summarize_fault_trials(trials: Iterable[FaultTrial]) -> dict[str, Any]:
    records = list(trials)
    for trial in records:
        trial.validate()
    recoverable = [item for item in records if item.expected == "recover" and item.injection.get("observed")]
    side_effect = [item for item in records if item.result.get("duplicate_side_effects") is not None and item.scenario.startswith(("F04", "F05"))]
    cancels = [item for item in records if item.scenario.startswith("F14") and item.injection.get("observed")]
    timeouts = [item for item in records if item.scenario.startswith("F13") and item.injection.get("observed")]
    completed = [item for item in records if item.injection.get("observed")]
    fail_closed = [item for item in records if item.expected == "fail-closed" and item.injection.get("observed")]
    metrics = {
        "resume_success_rate": _rate(sum(bool(item.result["resume_succeeded"] and item.result["verification_passed"]) for item in recoverable), len(recoverable)),
        "duplicate_side_effect_rate": _rate(sum(int(item.result["duplicate_side_effects"]) > 0 for item in side_effect), len(side_effect)),
        "cancellation_reclaim_rate": _rate(sum(item.result["orphan_processes"] == 0 and item.result["verification_passed"] for item in cancels), len(cancels)),
        "timeout_reclaim_rate": _rate(sum(item.result["orphan_processes"] == 0 and item.result["state_consistent"] for item in timeouts), len(timeouts)),
        "trace_completeness": _rate(sum(bool(item.result["trace_complete"]) for item in completed), len(completed)),
        "state_consistency_rate": _rate(sum(bool(item.result["state_consistent"]) for item in completed), len(completed)),
        "orphan_run_rate": _rate(sum(item.result["orphan_processes"] > 0 for item in completed), len(completed)),
        "false_success_rate": _rate(sum(bool(item.result["false_success"]) for item in completed), len(completed)),
        "fail_closed_correctness_rate": _rate(
            sum(bool(item.result["verification_passed"] and not item.result["resume_succeeded"]) for item in fail_closed),
            len(fail_closed),
        ),
    }
    latencies = sorted(float(item.result["recovery_latency_seconds"]) for item in recoverable)
    platforms = sorted({item.platform for item in records})
    p95 = latencies[min(len(latencies) - 1, int(0.95 * (len(latencies) - 1)))] if latencies else None
    return {
        "schema_version": FAULT_SCHEMA_VERSION,
        "suite_version": FAULT_SUITE_VERSION,
        "scope": (
            "smoke"
            if len(records) == len(FAULT_SCENARIOS)
            else "pilot"
            if len(records) == len(FAULT_SCENARIOS) * 5
            else "custom"
        ),
        "trials": len(records),
        "valid_trials": sum(bool(item.injection.get("observed")) for item in records),
        "invalid_trials": sum(not bool(item.injection.get("observed")) for item in records),
        "platforms": platforms,
        "multi_platform_evidence": len(platforms) > 1,
        "metrics": metrics,
        "recovery_latency_seconds": {
            "p50": median(latencies) if latencies else None,
            "p95": p95,
        },
        "scenarios": {
            name: {
                "trials": sum(item.scenario == name for item in records),
                "observed": sum(item.scenario == name and bool(item.injection.get("observed")) for item in records),
                "expected": SCENARIO_BY_NAME[name].expected,
            }
            for name in SCENARIO_BY_NAME
        },
        "thresholds": {
            "resume_success_rate": 0.95,
            "duplicate_side_effect_rate": 0.0,
            "cancellation_reclaim_rate": 0.99,
            "timeout_reclaim_rate": 0.99,
            "trace_completeness": 0.99,
            "state_consistency_rate": 0.99,
            "orphan_run_rate": 0.01,
            "false_success_rate": 0.0,
            "fail_closed_correctness_rate": 1.0,
        },
        "limitations": [
            "Smoke and pilot results are not the formal 460-trial matrix.",
            "F05 demonstrates the local fail-closed boundary; it cannot prove exactly-once behavior for an arbitrary external service.",
            "Docker unavailability is represented by an explicit deterministic fail-closed probe; no daemon is disrupted by this runner.",
        ],
    }


def run_fault_matrix(
    *,
    scenarios: Iterable[FaultScenario] = FAULT_SCENARIOS,
    trials_per_scenario: int = 1,
    out_dir: str | Path = "runs/fault-injection/v1",
    seed: int = 0,
    timeout_seconds: float = 20.0,
) -> tuple[list[FaultTrial], dict[str, Any]]:
    if trials_per_scenario < 1:
        raise ValueError("trials_per_scenario must be >= 1")
    selected = list(scenarios)
    records: list[FaultTrial] = []
    for scenario in selected:
        for trial in range(trials_per_scenario):
            records.append(
                run_fault_trial(
                    scenario,
                    trial=trial,
                    seed=seed + trial,
                    out_dir=out_dir,
                    timeout_seconds=timeout_seconds,
                )
            )
    summary = summarize_fault_trials(records)
    _json_write(Path(out_dir).resolve() / "summary.json", summary)
    return records, summary


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="AgentForge fault-injection controller/worker")
    parser.add_argument("--worker", action="store_true")
    parser.add_argument("--sleep-child", default=None)
    parser.add_argument("--sleep-leaf", action="store_true")
    parser.add_argument("--scenario", choices=sorted(SCENARIO_BY_NAME), default=None)
    parser.add_argument("--trial-root", default=None)
    parser.add_argument("--run-id", default=None)
    parser.add_argument("--resume", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    if args.sleep_leaf:
        return _sleep_leaf_main()
    if args.sleep_child:
        return _sleep_child_main(args.sleep_child)
    if args.worker:
        if not args.scenario or not args.trial_root or not args.run_id:
            raise SystemExit("--worker requires --scenario, --trial-root and --run-id")
        return _worker_main(args.scenario, args.trial_root, args.run_id, resume=args.resume)
    raise SystemExit("fault_injection is a library; use scripts/run_fault_matrix.py")


if __name__ == "__main__":
    raise SystemExit(main())
