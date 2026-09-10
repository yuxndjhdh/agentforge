"""闭环自检：任务完成后用权威 check 验真，未过则把失败反馈回喂给同一 agent 重试。

复用 `run(task, reset=False)` 的断点续跑机制（在 memory.steps 追加 TaskStep）——因此闭环
无需触碰 smolagents 执行循环，只在 harness 层编排：agent 在**同一份副本**上不断修改，
每轮用 `eval_reward` 作权威判定，未过时把 `failure_feedback` 的详情作为新任务塞回。

`max_attempts=1` 时退化为普通一次跑（与 `run`/`eval` 行为一致），用于对比/向后兼容。
"""

from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from .config import ModelConfig
from .eval import CodeTask, _execute_episode
from .harness import _now_id, make_agent
from .trace import RunTrace


@dataclass
class RunVerifiedResult:
    run_id: str
    task: str
    success: bool
    attempts: list[dict] = field(default_factory=list)
    trace: RunTrace | None = None
    trace_path: Path | None = None
    workdir: str = ""
    cleaned_up: bool = False


def _feedback_prompt(instruction: str, feedback: str) -> str:
    return f"{instruction}\n\n[自检未通过，请修正后重试]\n{feedback}"


def run_verified(
    cfg: ModelConfig,
    task: str,
    verify_task: CodeTask,
    *,
    max_attempts: int = 3,
    out_dir: str | Path = "runs/verify",
    agent=None,
    workdir: str | None = None,
    keep_workdir: bool = False,
) -> RunVerifiedResult:
    """闭环：复用评测层的 episode executor 反复修任务直到通过或触顶。"""
    verify_task.validate()
    if max_attempts < 1:
        raise ValueError("max_attempts must be >= 1")
    owned_workdir = workdir is None
    if owned_workdir:
        temp_root = tempfile.TemporaryDirectory(prefix="agentforge-verify-")
        wd = temp_root.name
        verify_task.build_seed(wd)
    else:
        temp_root = None
        assert workdir is not None
        wd = os.path.realpath(workdir)
    if agent is None:
        agent = make_agent(cfg, wd)
    try:
        result = _execute_episode(
            cfg,
            verify_task,
            instruction=task,
            workdir=wd,
            agent=agent,
            out_dir=Path(out_dir),
            verify_enabled=True,
            max_attempts=max_attempts,
        )
        return RunVerifiedResult(
            run_id=result.trace.run_id if result.trace else _now_id(),
            task=task,
            success=bool(result.reward),
            attempts=result.attempts,
            trace=result.trace,
            trace_path=result.trace_path,
            workdir=wd,
            cleaned_up=bool(owned_workdir and not keep_workdir),
        )
    finally:
        if temp_root is not None and not keep_workdir:
            temp_root.cleanup()
