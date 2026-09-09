"""Harness：用代码工具 + OpenAI 兼容模型组装 ToolCallingAgent，运行并落盘 trace。

断点续跑（MVP 的化）：
- `run_sequence` 让**同一个 agent 实例**依次处理多个任务——首个 `reset=True`，
  后续 `run(task, reset=False)` 在后记忆上继续，即"断点续跑"机制；每步落一个 trace。
- 跨进程的完整 transcript 重建目前依赖 smolagents 内部 memory 序列化（非公开 API），
  已在 README「已知要点与坑」里注明为后续迭代点。
"""

from __future__ import annotations

import os
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from .config import ModelConfig
from .llm import build_model
from .memory import memory_instructions
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


def make_agent(cfg: ModelConfig, workdir: str):
    """Build a ToolCallingAgent bound to code-domain tools for `workdir`.

    durable 记忆经 `instructions` 注入系统提示：换进程重跑时 agent 能「记得」跨会话知识。
    """
    from smolagents import ToolCallingAgent

    model = build_model(cfg)
    sandbox = Sandbox.from_config(cfg)
    return ToolCallingAgent(
        tools=all_code_tools(workdir, sandbox),
        model=model,
        max_steps=cfg.max_steps,
        instructions=memory_instructions(workdir),
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
    if agent is None:
        agent = make_agent(cfg, workdir)
    answer = agent.run(task, reset=reset)
    run_id = _now_id()
    trace = RunTrace.from_smol_agent(
        agent,
        run_id=run_id,
        workdir=os.path.realpath(workdir),
        task=task,
        model=cfg.model,
    )
    trace_path = trace.dump(Path(out_dir) / run_id / "trace.json")
    return RunResult(run_id=run_id, answer=str(answer), trace=trace, trace_path=trace_path)


def run_sequence(cfg: ModelConfig, tasks: list[str], workdir: str, *, out_dir: str | Path = "runs"):
    """同 agent 依次处理多个任务：演示断点续跑（后续任务 reset=False）。"""
    agent = make_agent(cfg, workdir)
    results = []
    for i, task in enumerate(tasks):
        results.append(
            run_task(cfg, task, workdir, out_dir=out_dir, agent=agent, reset=(i == 0))
        )
    return results


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
