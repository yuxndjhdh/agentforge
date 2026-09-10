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
from .eval import CodeTask, eval_reward, failure_feedback
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
    """闭环：同一 agent 在新鲜副本上反复修任务直到权威 check 通过或触顶。

    `workdir` 缺省时自建一份全新副本（build_seed）；注入时跳过 build_seed，便于测试绑定。
    """
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

    run_id = _now_id()
    attempts: list[dict] = []
    prev_feedback = ""
    success = False
    if max_attempts < 1:
        raise ValueError("max_attempts must be >= 1")
    trace = RunTrace(
        run_id=run_id,
        workdir=os.path.realpath(wd),
        task=task,
        model=cfg.model,
        status="running",
    )
    try:
        for attempt in range(max_attempts):
            prompt = _feedback_prompt(task, prev_feedback) if attempt > 0 else task
            before = len(getattr(getattr(agent, "memory", None), "steps", []) or [])
            answer = agent.run(prompt, reset=(attempt == 0))
            after = RunTrace.from_smol_agent(
                agent,
                run_id=run_id,
                workdir=os.path.realpath(wd),
                task=task,
                model=cfg.model,
                start_index=before,
            )
            trace.steps.extend(after.steps)
            reward = eval_reward(wd, verify_task)
            feedback = failure_feedback(wd, verify_task)
            attempts.append(
                {"attempt": attempt, "reward": reward, "feedback": feedback, "answer": str(answer)[:200]}
            )
            trace.add_manual(
                "verify",
                attempt=attempt,
                reward=reward,
                feedback=feedback,
            )
            if reward == 1:
                success = True
                break
            prev_feedback = feedback
        trace.status = "succeeded" if success else "failed"
        trace_path = trace.dump(Path(out_dir) / run_id / "trace.json")
        return RunVerifiedResult(
            run_id=run_id,
            task=task,
            success=success,
            attempts=attempts,
            trace=trace,
            trace_path=trace_path,
            workdir=wd,
            cleaned_up=bool(owned_workdir and not keep_workdir),
        )
    finally:
        if temp_root is not None and not keep_workdir:
            temp_root.cleanup()
