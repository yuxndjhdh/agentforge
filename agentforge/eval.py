"""终态 hash 评测层。

给定一个代码仓库任务（种子 + gold），让 agent 在**一份全新副本**上运行，然后用
最终仓库状态判定对错：二值 reward。判定支持两种 gold，可并用、需同时通过：
- 严格 hash：`gold_tree`（期望终态文件树），`实际树 hash == gold 树 hash`。
- 验收 checks：`file_contains / command(stdout_contains) / file_absent`，对最终状态逐条断言
  （Final Gate 式；内置演示任务用它，鲁棒、不受 LLM 措辞/格式影响）。

批量跑出 pass@1 / pass@k（组合估计器，照 τ-bench），并做规则版失败归因。
借 self agent-eval-harness 已验证的思路；这里是"状态对象 = 仓库文件树"的代码域版。
"""

from __future__ import annotations

import hashlib
import math
import os
import shlex
import tempfile
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Sequence

from .config import ModelConfig
from .harness import _now_id, make_agent
from .paths import resolve_path
from .sandbox import Sandbox
from .trace import RunTrace

DEFAULT_IGNORED = (".git", ".venv", "__pycache__", "node_modules", ".pytest_cache", ".env")

EMPTY_TREE = "empty-tree"


def _skip(name: str, ignored: tuple[str, ...]) -> bool:
    return name in ignored or name.startswith(".")


def hash_tree(workdir: str, ignored: tuple[str, ...] = DEFAULT_IGNORED) -> str:
    """对仓库文件树做确定性哈希：每个文件 sha256(relpath \\0 content)，汇总排序。"""
    root = resolve_path(workdir, ".", allow_missing=False).path
    if root is None:
        return EMPTY_TREE
    snapshots: list[tuple[str, bytes]] = []
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = [d for d in dirnames if not _skip(d, ignored)]
        for fn in filenames:
            if _skip(fn, ignored):
                continue
            fp = os.path.join(dirpath, fn)
            if os.path.islink(fp):
                continue
            try:
                if os.stat(fp, follow_symlinks=False).st_nlink > 1:
                    continue
            except OSError:
                continue
            safe = resolve_path(
                root,
                os.path.relpath(fp, root),
                allow_missing=False,
                reject_symlink=True,
                reject_hardlink=True,
            )
            if safe.path is None:
                continue
            try:
                with open(safe.path, "rb") as f:
                    snapshots.append((os.path.relpath(safe.path, root).replace(os.sep, "/"), f.read()))
            except OSError:
                continue
    return _hash_snapshots(snapshots)


def hash_tree_dict(tree: dict[str, str], ignored: tuple[str, ...] = DEFAULT_IGNORED) -> str:
    """对 gold_tree（path -> content）直接做同样的树哈希，无需落盘。"""
    snapshots: list[tuple[str, bytes]] = []
    for rel in sorted(tree):
        if not isinstance(rel, str) or not _valid_relative_name(rel):
            continue
        if any(_skip(part, ignored) for part in Path(rel).parts):
            continue
        snapshots.append((rel.replace("\\", "/"), tree[rel].encode("utf-8")))
    return _hash_snapshots(snapshots)


def _hash_snapshots(snapshots: list[tuple[str, bytes]]) -> str:
    if not snapshots:
        return EMPTY_TREE
    acc = hashlib.sha256()
    for rel, content in sorted(snapshots, key=lambda x: x[0]):
        acc.update(hashlib.sha256(rel.encode("utf-8") + b"\x00" + content).hexdigest().encode("utf-8"))
        acc.update(b"\x00")
    return acc.hexdigest()


def _valid_relative_name(rel: str) -> bool:
    if not rel or "\x00" in rel or os.path.isabs(rel):
        return False
    normalized = rel.replace("\\", "/")
    parts = [part for part in normalized.split("/") if part not in ("", ".")]
    return bool(parts) and ".." not in parts


@dataclass
class Check:
    kind: str  # "file_contains" | "command" | "file_absent"
    path: str = ""
    needles: list[str] = field(default_factory=list)
    command: str | Sequence[str] = ""
    stdout_contains: list[str] = field(default_factory=list)
    stdout_exact: str | None = None
    stdout_lines: list[str] | None = None
    stderr_contains: list[str] = field(default_factory=list)
    stdout_not_contains: list[str] = field(default_factory=list)
    timeout: float = 30.0


@dataclass(frozen=True)
class CommandCheckResult:
    """保留 stdout/stderr 分离语义的验收命令结果。"""

    returncode: int | None
    stdout: str
    stderr: str
    timed_out: bool = False
    output_limited: bool = False
    error: str | None = None


def _command_argv(command: str | Sequence[str]) -> list[str]:
    if isinstance(command, str):
        # 验收命令来自任务定义，不来自模型；仍使用 shell=False，避免验收器
        # 意外继承宿主 shell 的重定向、环境和控制语义。
        return shlex.split(command, posix=True)
    return [str(part) for part in command]


def run_check_command(
    command: str | Sequence[str],
    workdir: str,
    *,
    timeout: float = 30.0,
) -> CommandCheckResult:
    """在受限工作目录中运行一条可信验收命令（不启用 shell）。"""
    try:
        argv = _command_argv(command)
    except ValueError as exc:
        return CommandCheckResult(None, "", "", error=f"invalid command: {exc}")
    if not argv:
        return CommandCheckResult(None, "", "", error="empty command")
    result = Sandbox(
        timeout=max(0.01, float(timeout)),
        backend="local",
        max_output_bytes=256_000,
    ).execute(argv, cwd=workdir)
    return CommandCheckResult(
        result.returncode,
        result.stdout,
        result.stderr,
        timed_out=result.timed_out,
        output_limited=result.output_limited,
        error=result.error,
    )


def check_passes(check: Check, workdir: str) -> bool:
    if check.kind == "file_contains":
        resolved = resolve_path(
            workdir,
            check.path,
            allow_missing=False,
            reject_symlink=True,
            reject_hardlink=True,
        )
        if not resolved.path or not os.path.isfile(resolved.path):
            return False
        try:
            data = open(resolved.path, encoding="utf-8", errors="replace").read()
        except OSError:
            return False
        return all(n in data for n in check.needles)
    if check.kind == "file_absent":
        # lexists 也会把悬空符号链接视为存在；验收不能借此把越界链接
        # 当作“文件不存在”。
        resolved = resolve_path(workdir, check.path, allow_missing=True, reject_symlink=True)
        if resolved.path is None:
            return False
        return not os.path.lexists(resolved.path)
    if check.kind == "command":
        result = run_check_command(check.command, workdir, timeout=check.timeout)
        if result.error or result.timed_out or result.returncode != 0:
            return False
        if not all(s in result.stdout for s in check.stdout_contains):
            return False
        if check.stdout_exact is not None and result.stdout != check.stdout_exact:
            return False
        if check.stdout_lines is not None and result.stdout.splitlines() != check.stdout_lines:
            return False
        if not all(s in result.stderr for s in check.stderr_contains):
            return False
        return all(s not in result.stdout for s in check.stdout_not_contains)
    return False


@dataclass
class CodeTask:
    name: str
    instruction: str
    build_seed: Callable[[str], None]  # 往一份全新 workdir 写基线文件
    gold_tree: dict[str, str] | None = None
    checks: list[Check] = field(default_factory=list)
    ignored: tuple[str, ...] = DEFAULT_IGNORED
    difficulty: str = "medium"
    tags: tuple[str, ...] = ()
    resource_limits: dict[str, float | int] = field(default_factory=dict)
    source: str = "agentforge-seed"
    task_version: str = "1"
    gold_patch: str | None = None
    _gold_hash: str | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        self.validate()

    def validate(self) -> None:
        if not self.name or not self.name.strip():
            raise ValueError("CodeTask name cannot be empty")
        if not callable(self.build_seed):
            raise ValueError(f"CodeTask {self.name!r} requires a callable build_seed")
        if self.difficulty not in {"easy", "medium", "hard"}:
            raise ValueError(f"unsupported difficulty: {self.difficulty!r}")
        if not self.source.strip() or not self.task_version.strip():
            raise ValueError("CodeTask source and task_version cannot be empty")
        if self.gold_tree is None and not self.checks:
            raise ValueError(
                f"CodeTask {self.name!r} must define gold_tree or at least one check"
            )
        valid_kinds = {"file_contains", "command", "file_absent"}
        for check in self.checks:
            if check.kind not in valid_kinds:
                raise ValueError(f"unsupported check kind: {check.kind!r}")
            if check.kind == "file_contains" and not check.path:
                raise ValueError("file_contains check requires path")
            if check.kind == "file_absent" and not check.path:
                raise ValueError("file_absent check requires path")
            if check.kind == "command" and not check.command:
                raise ValueError("command check requires command")
            if check.timeout <= 0:
                raise ValueError("command check timeout must be positive")
        if self.gold_tree is not None:
            for rel in self.gold_tree:
                if not isinstance(rel, str) or not _valid_relative_name(rel):
                    raise ValueError(f"gold_tree path escapes workdir: {rel!r}")

    def to_spec(self) -> dict:
        return {
            "name": self.name,
            "instruction": self.instruction,
            "difficulty": self.difficulty,
            "tags": list(self.tags),
            "resource_limits": dict(self.resource_limits),
            "source": self.source,
            "task_version": self.task_version,
            "has_gold_tree": self.gold_tree is not None,
            "has_gold_patch": self.gold_patch is not None,
            "checks": [
                {
                    "kind": check.kind,
                    "path": check.path,
                    "command": check.command,
                    "needles": list(check.needles),
                    "stdout_contains": list(check.stdout_contains),
                    "stdout_exact": check.stdout_exact,
                    "stdout_lines": list(check.stdout_lines) if check.stdout_lines is not None else None,
                    "stderr_contains": list(check.stderr_contains),
                    "stdout_not_contains": list(check.stdout_not_contains),
                    "timeout": check.timeout,
                }
                for check in self.checks
            ],
        }

    def gold_hash(self) -> str | None:
        if self._gold_hash is None:
            self._gold_hash = (
                hash_tree_dict(self.gold_tree, self.ignored) if self.gold_tree is not None else None
            )
        return self._gold_hash


def eval_reward(workdir: str, task: CodeTask) -> int:
    task.validate()
    if resolve_path(workdir, ".", allow_missing=False).path is None:
        return 0
    ok = True
    if task.gold_tree is not None:
        ok = ok and hash_tree(workdir, task.ignored) == task.gold_hash()
    if task.checks:
        ok = ok and all(check_passes(c, workdir) for c in task.checks)
    return 1 if ok else 0


@dataclass
class EvalResult:
    task: str
    reward: int
    workdir: str
    answer: str
    trace: RunTrace | None = None
    diff: list[dict] | None = None
    trace_path: Path | None = None
    cleaned_up: bool = False
    elapsed_seconds: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0


def solve_task(
    cfg: ModelConfig,
    task: CodeTask,
    *,
    out_dir: str | Path = "runs/eval",
    keep_workdir: bool = False,
) -> EvalResult:
    """在全新副本上运行并评测任务，默认在返回前清理临时目录。"""
    task.validate()
    temp_root = tempfile.TemporaryDirectory(prefix="agentforge-eval-")
    wd = temp_root.name
    started = time.perf_counter()
    try:
        task.build_seed(wd)
        agent = make_agent(cfg, wd)
        answer = agent.run(task.instruction)
        reward = eval_reward(wd, task)
        run_id = _now_id()
        trace = RunTrace.from_smol_agent(
            agent,
            run_id=run_id,
            workdir=os.path.realpath(wd),
            task=task.instruction,
            model=cfg.model,
        )
        diff = tree_diff(wd, task) if reward == 0 else None
        trace_path = trace.dump(Path(out_dir) / task.name / run_id / "trace.json")
        action_steps = [step for step in trace.steps if step.get("kind") == "action"]
        input_tokens = sum(
            int((step.get("token_usage") or {}).get("input") or 0) for step in action_steps
        )
        output_tokens = sum(
            int((step.get("token_usage") or {}).get("output") or 0) for step in action_steps
        )
        return EvalResult(
            task.name,
            reward,
            wd,
            str(answer),
            trace,
            diff,
            trace_path,
            cleaned_up=not keep_workdir,
            elapsed_seconds=time.perf_counter() - started,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
        )
    finally:
        if not keep_workdir:
            temp_root.cleanup()


def tree_diff(wd: str, task: CodeTask) -> list[dict]:
    """失败时给出：gold_tree 任务 -> 路径级 diff；checks 任务 -> 未满足的检查。"""
    if task.gold_tree is not None:
        actual = _read_tree(wd, task.ignored)
        gold = task.gold_tree
        entries: list[dict] = []
        for rel in sorted(set(actual) | set(gold)):
            a, g = actual.get(rel), gold.get(rel)
            if g is None:
                entries.append({"path": rel, "state": "extra"})
            elif a is None:
                entries.append({"path": rel, "state": "missing"})
            elif a != g:
                entries.append({"path": rel, "state": "different"})
        return entries[:40]
    return [
        {"kind": c.kind, "target": c.path or c.command}
        for c in task.checks
        if not check_passes(c, wd)
    ]


def failure_feedback(wd: str, task: CodeTask) -> str:
    """对未通过的验收给出可读中文反馈（供闭环回喂给 agent）。全过则返回空串。"""
    if task.checks:
        lines = []
        for c in task.checks:
            if check_passes(c, wd):
                continue
            if c.kind == "file_contains":
                lines.append(f"- {c.path} 应包含 {', '.join(c.needles)}")
            elif c.kind == "command":
                expected = c.stdout_lines or c.stdout_contains
                lines.append(f"- 命令 {c.command!r} 输出应满足 {expected}")
            elif c.kind == "file_absent":
                lines.append(f"- {c.path} 应不存在")
        return "\n".join(lines)
    if task.gold_tree is not None:
        lines = [f"- 文件 {e['path']} 与期望不符（{e['state']}）" for e in tree_diff(wd, task)]
        return "\n".join(lines)
    return ""


def _read_tree(wd: str, ignored: tuple[str, ...]) -> dict[str, str]:
    out: dict[str, str] = {}
    root = resolve_path(wd, ".", allow_missing=False).path
    if root is None:
        return out
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = [d for d in dirnames if not _skip(d, ignored)]
        for fn in filenames:
            if _skip(fn, ignored):
                continue
            fp = os.path.join(dirpath, fn)
            if os.path.islink(fp):
                continue
            try:
                if os.stat(fp, follow_symlinks=False).st_nlink > 1:
                    continue
            except OSError:
                continue
            safe = resolve_path(
                root,
                os.path.relpath(fp, root),
                allow_missing=False,
                reject_symlink=True,
                reject_hardlink=True,
            )
            if safe.path is None:
                continue
            try:
                out[os.path.relpath(safe.path, root).replace(os.sep, "/")] = open(
                    safe.path, encoding="utf-8", errors="replace"
                ).read()
            except OSError:
                continue
    return out


def analyze_failure(task: CodeTask, result: EvalResult) -> dict:
    steps = [s for s in (result.trace.steps if result.trace else []) if s.get("kind") == "action"]
    errs = []
    for step in steps:
        calls = step.get("tool_calls") or []
        if str(step.get("observation", "")).startswith("Error:"):
            errs.append(
                {
                    "tool": calls[0].get("name", "unknown") if calls else "unknown",
                    "obs": str(step.get("observation", ""))[:120],
                }
            )
    if not steps:
        verdict = "did_not_act"
    elif errs:
        verdict = f"tool_error({errs[0]['tool']}): {errs[0]['obs']}"
    elif result.diff:
        verdict = f"final_state_mismatch: {result.diff[:3]}"
    else:
        verdict = "failed(unknown)"
    return {
        "task": task.name,
        "verdict": verdict,
        "tool_errors": errs,
        "diff": result.diff,
        "final_answer": result.answer[:200],
    }


def evaluate(
    cfg: ModelConfig,
    tasks: list[CodeTask],
    *,
    num_trials: int = 1,
    k: int = 1,
    out_dir: str | Path = "runs/eval",
) -> dict:
    if num_trials < 1:
        raise ValueError("num_trials must be >= 1")
    if not 1 <= k <= num_trials:
        raise ValueError("k must satisfy 1 <= k <= num_trials")
    for task in tasks:
        task.validate()
    names = [task.name for task in tasks]
    if len(set(names)) != len(names):
        raise ValueError("task names must be unique")
    success: Counter[str] = Counter({name: 0 for name in names})
    failures: list[dict] = []
    episodes: list[dict] = []
    for task in tasks:
        for trial in range(num_trials):
            res = solve_task(cfg, task, out_dir=out_dir)
            if res.reward == 1:
                success[task.name] += 1
            elif len(failures) < 5:
                failures.append(analyze_failure(task, res))
            episodes.append(
                {
                    "task": task.name,
                    "trial": trial,
                    "reward": res.reward,
                    "answer": res.answer[:200],
                    "trace_path": str(res.trace_path) if res.trace_path else None,
                    "elapsed_seconds": res.elapsed_seconds,
                    "steps": len([step for step in (res.trace.steps if res.trace else []) if step.get("kind") == "action"]),
                    "input_tokens": res.input_tokens,
                    "output_tokens": res.output_tokens,
                    "failure_type": _failure_type(task, res),
                }
            )
    n = len(tasks)
    report = {
        "tasks": n,
        "num_trials": num_trials,
        "success": dict(success),
        "pass@1": pass_at_k(dict(success), num_trials, 1) if n else 0.0,
        "pass@k": pass_at_k(success, num_trials, k),
        "failures": failures,
        "episodes": episodes,
    }
    for requested_k in (3, 5):
        if requested_k <= num_trials:
            report[f"pass@{requested_k}"] = pass_at_k(success, num_trials, requested_k)
    return report


def _failure_type(task: CodeTask, result: EvalResult) -> str | None:
    if result.reward:
        return None
    steps = [step for step in (result.trace.steps if result.trace else []) if step.get("kind") == "action"]
    if not steps:
        return "did_not_act"
    if any(str(step.get("observation", "")).startswith("Error:") for step in steps):
        return "tool_error"
    if result.elapsed_seconds and result.elapsed_seconds >= 0:
        # The executor already classifies timeout failures through its trace
        # observation; keep this bucket deterministic for evaluator output.
        if any("timed out" in str(step.get("observation", "")).lower() for step in steps):
            return "timeout"
    return "state_mismatch"


def _comb(n: int, k: int) -> int:
    if k < 0 or k > n:
        return 0
    return math.comb(n, k)


def pass_at_k(success_counts: dict[str, int], num_trials: int, k: int) -> float:
    """按标准定义计算“至少一次成功”的组合估计器。

    对每个任务，``c`` 是 N 次独立 episode 中成功的次数：
    ``1 - C(N-c, k) / C(N, k)``。成功次数为零的任务必须保留在输入中，
    否则平均值会被错误抬高。
    """
    if num_trials < 1:
        raise ValueError("num_trials must be >= 1")
    if not 1 <= k <= num_trials:
        raise ValueError("k must satisfy 1 <= k <= num_trials")
    if not success_counts:
        return 0.0
    denominator = _comb(num_trials, k)
    if denominator == 0:  # defensive; validation above makes this unreachable
        raise ValueError("invalid pass@k denominator")
    probabilities = []
    for task, count in success_counts.items():
        if not isinstance(count, int) or isinstance(count, bool):
            raise ValueError(f"success count for {task!r} must be an integer")
        if count < 0 or count > num_trials:
            raise ValueError(f"success count for {task!r} must be between 0 and num_trials")
        probabilities.append(1.0 - (_comb(num_trials - count, k) / denominator))
    total = sum(probabilities)
    return total / len(success_counts)
