"""闭环自检层单测（无需 LLM）：stub agent 驱动 run_verified 的闭环逻辑 + 反馈格式。"""

from __future__ import annotations

import os
from types import SimpleNamespace

from agentforge.config import ModelConfig
from agentforge.eval import Check, CodeTask, failure_feedback
from agentforge.verify import run_verified

MODEL_OK = "def add(a, b):\n    return a + b\n"
MODEL_BUGGY = "def add(a, b):\n    return a - b\n"

CFG = ModelConfig(base_url="", api_key="", model="")

# 闭环逻辑测试用：仅 file_contains，让 stub agent 能真正“改对”
VERIFY_TASK = CodeTask(
    name="verify-add",
    instruction="fix calc",
    build_seed=lambda wd: None,
    checks=[Check(kind="file_contains", path="calc.py", needles=["return a + b"])],
)


class FakeAgent:
    """记录调用、可让 wd 在指定次数调用后“变对”的假 agent。"""

    def __init__(self, wd, fix_on_call=1):
        self.wd = wd
        self.fix_on_call = fix_on_call
        self.calls = []
        self.memory = SimpleNamespace(steps=[])  # 供 RunTrace.from_smol_agent 抽取
        self.n = 0

    def run(self, prompt, reset=True):
        self.calls.append({"prompt": prompt, "reset": reset})
        self.n += 1
        if self.n >= self.fix_on_call:
            with open(os.path.join(self.wd, "calc.py"), "w", encoding="utf-8") as f:
                f.write(MODEL_OK)
        return "done"


def _write(wd, content):
    os.makedirs(wd, exist_ok=True)
    with open(os.path.join(wd, "calc.py"), "w", encoding="utf-8") as f:
        f.write(content)


def test_retry_loop_feeds_feedback_then_stops_on_success(tmp_path):
    _write(tmp_path, MODEL_BUGGY)
    agent = FakeAgent(str(tmp_path), fix_on_call=2)
    res = run_verified(
        CFG, VERIFY_TASK.instruction, VERIFY_TASK, max_attempts=3, agent=agent, workdir=str(tmp_path)
    )

    assert res.success is True
    assert len(agent.calls) == 2  # 第 2 轮即通过，提前止损
    assert agent.calls[0]["prompt"] == VERIFY_TASK.instruction
    assert agent.calls[0]["reset"] is True
    assert agent.calls[1]["reset"] is False
    assert "[自检未通过" in agent.calls[1]["prompt"]  # 反馈确实回喂
    assert res.attempts[0]["reward"] == 0
    assert "return a + b" in res.attempts[0]["feedback"]
    assert res.attempts[1]["reward"] == 1


def test_max_attempts_respected_when_never_fixed(tmp_path):
    _write(tmp_path, MODEL_BUGGY)
    agent = FakeAgent(str(tmp_path), fix_on_call=999)  # 永远不修对
    res = run_verified(
        CFG, VERIFY_TASK.instruction, VERIFY_TASK, max_attempts=4, agent=agent, workdir=str(tmp_path)
    )
    assert res.success is False
    assert len(agent.calls) == 4
    assert all(a["reward"] == 0 for a in res.attempts)


def test_single_attempt_passthrough(tmp_path):
    _write(tmp_path, MODEL_OK)  # 副本本来就对
    agent = FakeAgent(str(tmp_path), fix_on_call=1)
    res = run_verified(
        CFG, VERIFY_TASK.instruction, VERIFY_TASK, max_attempts=1, agent=agent, workdir=str(tmp_path)
    )
    assert res.success is True
    assert len(agent.calls) == 1
    assert res.attempts[0]["reward"] == 1


def test_verify_steps_dumped_to_trace(tmp_path):
    _write(tmp_path, MODEL_BUGGY)
    agent = FakeAgent(str(tmp_path), fix_on_call=2)
    res = run_verified(
        CFG,
        VERIFY_TASK.instruction,
        VERIFY_TASK,
        max_attempts=3,
        out_dir=tmp_path / "out",
        agent=agent,
        workdir=str(tmp_path),
    )
    verify_steps = [s for s in res.trace.steps if s.get("kind") == "verify"]
    assert len(verify_steps) == 2  # 与运行轮数一致
    assert verify_steps[0]["reward"] == 0
    assert verify_steps[1]["reward"] == 1
    assert res.trace_path.exists()


def test_feedback_formats_checks(tmp_path):
    # 不同类型 check 的失败反馈格式
    os.makedirs(tmp_path, exist_ok=True)
    with open(os.path.join(tmp_path, "calc.py"), "w", encoding="utf-8") as f:
        f.write("return a - b")
    # file_absent 要“失败”需文件存在
    with open(os.path.join(tmp_path, "gone.py"), "w", encoding="utf-8") as f:
        f.write("should not exist")
    task = CodeTask(
        name="t",
        instruction="i",
        build_seed=lambda wd: None,
        checks=[
            Check(kind="file_contains", path="calc.py", needles=["return a + b"]),
            Check(kind="command", command="python main.py", stdout_contains=["5", "6"]),
            Check(kind="file_absent", path="gone.py"),
        ],
    )
    fb = failure_feedback(str(tmp_path), task)
    assert "calc.py 应包含 return a + b" in fb
    assert "python main.py" in fb
    assert "gone.py 应不存在" in fb


def test_feedback_empty_when_pass(tmp_path):
    _write(tmp_path, MODEL_OK)
    fb = failure_feedback(str(tmp_path), VERIFY_TASK)
    assert fb == ""
