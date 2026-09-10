"""Harness adapter shared by CLI, evaluator, and the independent runtime."""

from __future__ import annotations

import os
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from .config import ModelConfig
from .llm import build_model
from .memory import memory_instructions
from .runtime import AgentRuntime, RuntimeStore
from .sandbox import Sandbox
from .tools import all_code_tools
from .trace import RunTrace


@dataclass
class RunResult:
    run_id: str
    answer: str
    trace: RunTrace
    trace_path: Path


def _now_id() -> str:
    return time.strftime("%Y%m%d-%H%M%S") + f"-{int(time.time() * 1000) % 1000:03d}"


def make_agent(
    cfg: ModelConfig,
    workdir: str,
    *,
    event_sink=None,
    project_id: str = "default",
    user_id: str = "default",
    run_id: str | None = None,
    cancel_event=None,
):
    """Build a ToolCallingAgent bound to code-domain tools for `workdir`.

    durable 记忆经 `instructions` 注入系统提示：换进程重跑时 agent 能「记得」跨会话知识。
    """
    from smolagents import ToolCallingAgent

    model = build_model(cfg)
    sandbox = Sandbox.from_config(cfg)
    sandbox.decision_sink = (
        lambda event: event_sink({"phase": "policy", **event}) if event_sink else None
    )
    sandbox.cancel_event = cancel_event
    return ToolCallingAgent(
        tools=all_code_tools(
            workdir,
            sandbox,
            event_sink=event_sink,
            project_id=project_id,
            user_id=user_id,
            run_id=run_id,
        ),
        model=model,
        max_steps=cfg.max_steps,
        instructions=memory_instructions(workdir, project_id=project_id, user_id=user_id),
    )


def run_task(
    cfg: ModelConfig,
    task: str,
    workdir: str,
    *,
    out_dir: str | Path = "runs",
    agent=None,
    reset: bool = True,
) -> RunResult:
    """对 `workdir` 跑一个任务；`reset=False` 表示在同一 agent 上续跑。落盘 trace。"""
    runtime = AgentRuntime(
        RuntimeStore(cfg.state_db, out_dir),
        trace_root=out_dir,
    )
    try:
        result = runtime.run_agent(
            cfg,
            task,
            workdir,
            agent=agent,
            reset=reset,
        )
        return RunResult(
            run_id=result.run.id,
            answer=result.answer,
            trace=result.trace,
            trace_path=result.trace_path,
        )
    finally:
        runtime.store.close()


def run_sequence(cfg: ModelConfig, tasks: list[str], workdir: str, *, out_dir: str | Path = "runs"):
    """同一 agent 依次处理多个任务，且每次 trace 仅包含当前调用。"""
    runtime = AgentRuntime(RuntimeStore(cfg.state_db, out_dir), trace_root=out_dir)
    agent = None
    results = []
    try:
        for i, task in enumerate(tasks):
            result = runtime.run_agent(
                cfg,
                task,
                workdir,
                agent=agent,
                reset=(i == 0),
            )
            agent = result.agent
            results.append(
                RunResult(
                    run_id=result.run.id,
                    answer=result.answer,
                    trace=result.trace,
                    trace_path=result.trace_path,
                )
            )
        return results
    finally:
        runtime.store.close()


def run_selftest(workdir: str | None = None, out_dir: str | Path = "runs") -> RunResult:
    """无 LLM：用确定性动作序列跑工具链，验证 read/list/grep/write + run_command + trace 落盘。

    workdir 为空时自建一份临时样例仓库，避免污染 examples/。
    """
    if workdir is None:
        wd = tempfile.mkdtemp(prefix="agentforge-selftest-")
        _write_sample_repo(wd)
    else:
        wd = os.path.realpath(workdir)
    tools = {t.name: t for t in all_code_tools(wd)}
    trace = RunTrace(run_id="selftest", workdir=wd, task="selftest: deterministic tool sequence")
    steps = []

    obs = tools["list_dir"].forward(".")
    assert "a.py" in obs and "README.md" in obs, f"list_dir bad: {obs}"
    steps.append((1, "list_dir", {"path": "."}, obs))

    obs = tools["read_file"].forward("a.py")
    assert obs and "TODO" in obs, f"read_file bad: {obs}"
    steps.append((2, "read_file", {"path": "a.py"}, obs))

    obs = tools["grep"].forward("def |TODO", ".")
    assert obs and "a.py:1: def foo" in obs, f"grep bad: {obs}"
    steps.append((3, "grep", {"pattern": "def |TODO", "path": "."}, obs))

    obs = tools["write_file"].forward("out.txt", "agentforge selftest\n")
    assert obs and "Error" not in obs, f"write_file bad: {obs}"
    obs_back = tools["read_file"].forward("out.txt")
    assert "agentforge selftest" in obs_back, f"write/read roundtrip bad: {obs_back}"
    steps.append((4, "write_file", {"path": "out.txt", "content": "agentforge selftest"}, obs))

    obs_cmd = tools["run_command"].forward("python --version")
    assert "exit=" in obs_cmd, f"run_command bad: {obs_cmd}"
    steps.append((5, "run_command", {"command": "python --version"}, obs_cmd))

    for n, name, args, ob in steps:
        trace.add_manual("action", step=n, tool_calls=[{"name": name, "arguments": args}], observation=ob)
    trace.add_manual("final", output="selftest passed")

    trace_path = trace.dump(Path(out_dir) / "selftest" / "trace.json")
    return RunResult(run_id="selftest", answer="selftest passed", trace=trace, trace_path=trace_path)


def _write_sample_repo(wd: str) -> None:
    os.makedirs(wd, exist_ok=True)
    files = {
        "a.py": "def foo():\n    # TODO\n    return 1\n",
        "README.md": "# sample\n",
    }
    for rel, content in files.items():
        with open(os.path.join(wd, rel), "w", encoding="utf-8") as f:
            f.write(content)
