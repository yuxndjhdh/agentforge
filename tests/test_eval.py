"""评测层单测（无需 LLM）：hash_tree / reward / pass@k / 失败归因。"""

from __future__ import annotations

from types import SimpleNamespace

from agentforge.eval import (
    Check,
    CodeTask,
    analyze_failure,
    eval_reward,
    hash_tree,
    hash_tree_dict,
    pass_at_k,
)
from agentforge.trace import RunTrace


def _mk(tmp_path, tree):
    for rel, content in tree.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    return str(tmp_path)


def test_hash_tree_deterministic_and_differs(tmp_path):
    wd = _mk(tmp_path, {"a.txt": "hello", "sub/b.py": "x = 1"})
    h1 = hash_tree(wd)
    assert h1 == hash_tree(wd)
    (tmp_path / "a.txt").write_text("bye", encoding="utf-8")
    assert hash_tree(wd) != h1


def test_hash_tree_ignores_dot_and_ignored(tmp_path):
    wd = _mk(
        tmp_path,
        {
            "a.txt": "hello",
            ".git/config": "secret",
            ".venv/x.py": "x",
            ".env": "KEY=1",
            "__pycache__/m.pyc": "p",
        },
    )
    assert hash_tree(wd) == hash_tree_dict({"a.txt": "hello"})


def test_hash_tree_dict_equals_materialized(tmp_path):
    tree = {"a.txt": "hello", "sub/b.py": "x = 1"}
    wd = _mk(tmp_path, tree)
    assert hash_tree(wd) == hash_tree_dict(tree)


def test_eval_reward_checks(tmp_path):
    wd = str(tmp_path)
    task = CodeTask(
        name="t",
        instruction="i",
        build_seed=lambda w: None,
        checks=[Check(kind="file_contains", path="calc.py", needles=["return a + b"])],
    )
    (tmp_path / "calc.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    assert eval_reward(wd, task) == 1
    (tmp_path / "calc.py").write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")
    assert eval_reward(wd, task) == 0


def test_command_check_requires_zero_exit_and_stdout(tmp_path):
    task = CodeTask(
        name="command",
        instruction="i",
        build_seed=lambda w: None,
        checks=[Check(kind="command", command="python -c \"import sys; print('ok'); sys.exit(2)\"", stdout_contains=["ok"])],
    )
    assert eval_reward(str(tmp_path), task) == 0
    stderr_task = CodeTask(
        name="stderr",
        instruction="i",
        build_seed=lambda w: None,
        checks=[Check(kind="command", command="python -c \"import sys; print('ok', file=sys.stderr)\"", stdout_contains=["ok"])],
    )
    assert eval_reward(str(tmp_path), stderr_task) == 0


def test_command_check_can_assert_behavioral_stdout_lines(tmp_path):
    exact = CodeTask(
        name="exact",
        instruction="i",
        build_seed=lambda w: None,
        checks=[
            Check(
                kind="command",
                command="python -c \"print('5'); print('6')\"",
                stdout_lines=["5", "6"],
            )
        ],
    )
    assert eval_reward(str(tmp_path), exact) == 1
    wrong = CodeTask(
        name="wrong-exact",
        instruction="i",
        build_seed=lambda w: None,
        checks=[
            Check(
                kind="command",
                command="python -c \"print('15'); print('6')\"",
                stdout_lines=["5", "6"],
            )
        ],
    )
    assert eval_reward(str(tmp_path), wrong) == 0


def test_code_task_requires_acceptance_rule():
    import pytest

    with pytest.raises(ValueError):
        CodeTask(name="invalid", instruction="i", build_seed=lambda w: None)


def test_eval_reward_gold_tree(tmp_path):
    task = CodeTask(
        name="t",
        instruction="i",
        build_seed=lambda w: None,
        gold_tree={"calc.py": "return a + b"},
    )
    wd = _mk(tmp_path, {"calc.py": "return a + b"})
    assert eval_reward(wd, task) == 1
    _tmp2 = _mk(tmp_path, {"calc.py": "return a - b"})
    assert eval_reward(_tmp2, task) == 0


def test_pass_at_k():
    assert pass_at_k({"a": 1}, 1, 1) == 1.0
    assert pass_at_k({"a": 0}, 1, 1) == 0.0
    assert pass_at_k({"a": 2}, 2, 1) == 1.0
    assert pass_at_k({"a": 1}, 2, 2) == 1.0  # 至少一次成功
    assert abs(pass_at_k({"a": 1}, 2, 1) - 0.5) < 1e-9


def test_pass_at_k_keeps_zero_success_tasks_in_denominator():
    assert abs(pass_at_k({"success": 1, "failure": 0}, 2, 1) - 0.25) < 1e-9
    assert pass_at_k({"success": 1, "failure": 0}, 2, 2) == 0.5


def test_pass_at_k_validates_inputs():
    import pytest

    with pytest.raises(ValueError):
        pass_at_k({"a": 0}, 0, 1)
    with pytest.raises(ValueError):
        pass_at_k({"a": 0}, 2, 0)
    with pytest.raises(ValueError):
        pass_at_k({"a": 0}, 2, 3)


def test_analyze_failure_tool_error():
    tr = RunTrace(run_id="r", workdir="/w", task="t")
    tr.add_manual(
        "action",
        step=1,
        tool_calls=[{"name": "read_file", "arguments": {}}],
        observation="Error: no such file: x.py",
    )
    res = SimpleNamespace(task=SimpleNamespace(name="fix-add"), trace=tr, diff=None, answer="")
    out = analyze_failure(res.task, res)
    assert out["verdict"].startswith("tool_error")


def test_analyze_failure_did_not_act():
    tr = RunTrace(run_id="r", workdir="/w", task="t")
    res = SimpleNamespace(task=SimpleNamespace(name="fix-add"), trace=tr, diff=None, answer="")
    out = analyze_failure(res.task, res)
    assert out["verdict"] == "did_not_act"
