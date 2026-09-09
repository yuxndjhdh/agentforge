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
import subprocess
import tempfile
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .config import ModelConfig
from .harness import _now_id, make_agent
from .trace import RunTrace

DEFAULT_IGNORED = (".git", ".venv", "__pycache__", "node_modules", ".pytest_cache", ".env")

EMPTY_TREE = "empty-tree"


def _skip(name: str, ignored: tuple[str, ...]) -> bool:
    return name in ignored or name.startswith(".")


def hash_tree(workdir: str, ignored: tuple[str, ...] = DEFAULT_IGNORED) -> str:
    """对仓库文件树做确定性哈希：每个文件 sha256(relpath \\0 content)，汇总排序。"""
    snapshots: list[tuple[str, bytes]] = []
    for dirpath, dirnames, filenames in os.walk(workdir):
        dirnames[:] = [d for d in dirnames if not _skip(d, ignored)]
        for fn in filenames:
            if _skip(fn, ignored):
                continue
            fp = os.path.join(dirpath, fn)
            if os.path.islink(fp):
                continue
            with open(fp, "rb") as f:
                snapshots.append((os.path.relpath(fp, workdir).replace(os.sep, "/"), f.read()))
    return _hash_snapshots(snapshots)


def hash_tree_dict(tree: dict[str, str], ignored: tuple[str, ...] = DEFAULT_IGNORED) -> str:
    """对 gold_tree（path -> content）直接做同样的树哈希，无需落盘。"""
    snapshots: list[tuple[str, bytes]] = []
    for rel in sorted(tree):
        if any(_skip(part, ignored) for part in Path(rel).parts):
            continue
        snapshots.append((rel, tree[rel].encode("utf-8")))
    return _hash_snapshots(snapshots)


def _hash_snapshots(snapshots: list[tuple[str, bytes]]) -> str:
    if not snapshots:
        return EMPTY_TREE
    acc = hashlib.sha256()
    for rel, content in sorted(snapshots, key=lambda x: x[0]):
        acc.update(hashlib.sha256(rel.encode("utf-8") + b"\x00" + content).hexdigest().encode("utf-8"))
        acc.update(b"\x00")
    return acc.hexdigest()


@dataclass
class Check:
    kind: str  # "file_contains" | "command" | "file_absent"
    path: str = ""
    needles: list[str] = field(default_factory=list)
    command: str = ""
    stdout_contains: list[str] = field(default_factory=list)


def check_passes(check: Check, workdir: str) -> bool:
    if check.kind == "file_contains":
        fp = os.path.join(workdir, check.path)
        if not os.path.isfile(fp):
            return False
        data = open(fp, encoding="utf-8", errors="replace").read()
        return all(n in data for n in check.needles)
    if check.kind == "file_absent":
        return not os.path.exists(os.path.join(workdir, check.path))
    if check.kind == "command":
        try:
            cp = subprocess.run(
                check.command, shell=True, cwd=workdir, capture_output=True, text=True, timeout=30
            )
        except Exception:
            return False
        out = (cp.stdout or "") + (cp.stderr or "")
        return all(s in out for s in check.stdout_contains)
    return False


@dataclass
class CodeTask:
    name: str
    instruction: str
    build_seed: Callable[[str], None]  # 往一份全新 workdir 写基线文件
    gold_tree: dict[str, str] | None = None
    checks: list[Check] = field(default_factory=list)
    ignored: tuple[str, ...] = DEFAULT_IGNORED
    _gold_hash: str | None = field(default=None, repr=False)

    def gold_hash(self) -> str | None:
        if self._gold_hash is None:
            self._gold_hash = hash_tree_dict(self.gold_tree, self.ignored) if self.gold_tree else None
        return self._gold_hash


def eval_reward(workdir: str, task: CodeTask) -> int:
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


def solve_task(cfg: ModelConfig, task: CodeTask, *, out_dir: str | Path = "runs/eval") -> EvalResult:
    """在一份全新临时副本上跑任务，用终态判定 reward，失败时落盘 trace + diff。"""
    wd = tempfile.mkdtemp(prefix="agentforge-eval-")
    task.build_seed(wd)
    agent = make_agent(cfg, wd)
    answer = agent.run(task.instruction)
    reward = eval_reward(wd, task)
    run_id = _now_id()
    trace = RunTrace.from_smol_agent(
        agent, run_id=run_id, workdir=os.path.realpath(wd), task=task.instruction, model=cfg.model
    )
    diff = None
    trace_path = None
    if reward == 0:
        diff = tree_diff(wd, task)
        trace_path = trace.dump(Path(out_dir) / task.name / run_id / "trace.json")
    return EvalResult(task.name, reward, wd, str(answer), trace, diff, trace_path)


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
                lines.append(f"- 命令 {c.command!r} 输出应包含 {', '.join(c.stdout_contains)}")
            elif c.kind == "file_absent":
                lines.append(f"- {c.path} 应不存在")
        return "\n".join(lines)
    if task.gold_tree is not None:
        lines = [f"- 文件 {e['path']} 与期望不符（{e['state']}）" for e in tree_diff(wd, task)]
        return "\n".join(lines)
    return ""


def _read_tree(wd: str, ignored: tuple[str, ...]) -> dict[str, str]:
    out: dict[str, str] = {}
    for dirpath, dirnames, filenames in os.walk(wd):
        dirnames[:] = [d for d in dirnames if not _skip(d, ignored)]
        for fn in filenames:
            if _skip(fn, ignored):
                continue
            fp = os.path.join(dirpath, fn)
            if os.path.islink(fp):
                continue
            out[os.path.relpath(fp, wd).replace(os.sep, "/")] = open(
                fp, encoding="utf-8", errors="replace"
            ).read()
    return out


def analyze_failure(task: CodeTask, result: EvalResult) -> dict:
    steps = [s for s in (result.trace.steps if result.trace else []) if s.get("kind") == "action"]
    errs = [
        {"tool": s["tool_calls"][0]["name"], "obs": s.get("observation", "")[:120]}
        for s in steps
        if str(s.get("observation", "")).startswith("Error:")
    ]
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
    success: Counter[str] = Counter()
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
                }
            )
    n = len(tasks)
    denom = n * num_trials
    return {
        "tasks": n,
        "num_trials": num_trials,
        "success": dict(success),
        "pass@1": (sum(success.values()) / denom) if denom else 0.0,
        "pass@k": pass_at_k(success, num_trials, k),
        "failures": failures,
        "episodes": episodes,
    }


def _comb(n: int, k: int) -> int:
    if k < 0 or k > n:
        return 0
    return math.comb(n, k)


def pass_at_k(success_counts: dict[str, int], num_trials: int, k: int) -> float:
    """组合估计：pass@k = Σ_task C(c,k)/C(N,k) / #tasks。"""
    if not success_counts:
        return 0.0
    total = sum(_comb(c, k) / _comb(num_trials, k) for c in success_counts.values())
    return total / len(success_counts)
