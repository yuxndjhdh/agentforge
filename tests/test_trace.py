"""trace/checkpoint 的 JSON 往返 + from_smol_agent 抽取（fake agent，无需 smolagents）。"""

from __future__ import annotations

from types import SimpleNamespace

from agentforge.trace import RunTrace


def test_dump_load_roundtrip(tmp_path):
    tr = RunTrace(run_id="r1", workdir="/wd", task="t", model="m")
    tr.add_manual("action", step=1, tool_calls=[{"name": "read_file", "arguments": {"path": "a.py"}}], observation="content")
    p = tr.dump(tmp_path / "r1" / "trace.json")
    assert p.exists()
    loaded = RunTrace.load(p)
    assert loaded.run_id == "r1"
    assert loaded.steps[0]["kind"] == "action"
    assert loaded.steps[0]["tool_calls"][0]["name"] == "read_file"
    assert loaded.render().startswith("# Run r1")


def test_from_smol_agent_fake():
    class TaskStep:
        def __init__(self):
            self.task = "do thing"

    class ActionStep:
        step_number = 1
        tool_calls = [SimpleNamespace(name="read_file", arguments={"path": "a.py"})]
        observations = "file contents"
        token_usage = SimpleNamespace(input_tokens=10, output_tokens=5)

    class FinalAnswerStep:
        output = "done"

    agent = SimpleNamespace(memory=SimpleNamespace(steps=[TaskStep(), ActionStep(), FinalAnswerStep()]))
    tr = RunTrace.from_smol_agent(agent, run_id="r2", workdir="/wd", task="t", model="m")
    assert [s["kind"] for s in tr.steps] == ["task", "action", "final"]
    assert tr.steps[1]["tool_calls"][0]["name"] == "read_file"
    assert tr.steps[1]["token_usage"]["input"] == 10
    assert tr.steps[2]["output"] == "done"
