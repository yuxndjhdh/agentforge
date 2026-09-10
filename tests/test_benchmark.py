from __future__ import annotations

import json

from agentforge.benchmark import load_report, run_benchmark
from agentforge.code_tasks import BENCHMARK_TASKS, select_benchmark_tasks
from agentforge.config import ModelConfig
from agentforge.eval import Check, CodeTask


def _task(name: str, marker: str) -> CodeTask:
    def seed(workdir: str) -> None:
        with open(f"{workdir}/result.txt", "w", encoding="utf-8") as stream:
            stream.write(marker)

    return CodeTask(
        name=name,
        instruction="fake solver task",
        build_seed=seed,
        checks=[Check(kind="file_contains", path="result.txt", needles=[marker])],
        source="test",
        task_version="test-1",
    )


def test_select_benchmark_tasks_preserves_canonical_order_and_rejects_unknown():
    selected = select_benchmark_tasks([BENCHMARK_TASKS[3].name, BENCHMARK_TASKS[0].name])
    assert [task.name for task in selected] == [BENCHMARK_TASKS[0].name, BENCHMARK_TASKS[3].name]
    import pytest

    with pytest.raises(ValueError, match="unknown benchmark task"):
        select_benchmark_tasks(["does-not-exist"])


def test_benchmark_report_uses_fake_solver_and_writes_atomically(tmp_path, monkeypatch):
    tasks = [_task("pass", "PASS"), _task("fail", "PASS")]

    def fake_solve(cfg, task, *, out_dir, keep_workdir=False):
        from agentforge.eval import EvalResult
        from agentforge.trace import RunTrace

        reward = 1 if task.name == "pass" else 0
        trace = RunTrace(run_id=f"run-{task.name}", workdir=str(tmp_path), task=task.name)
        trace.add_manual("action", step=1, tool_calls=[], observation="ok")
        return EvalResult(
            task=task.name,
            reward=reward,
            workdir=str(tmp_path),
            answer="fake",
            trace=trace,
            diff=[] if reward else [{"kind": "file_contains"}],
            elapsed_seconds=0.25,
            input_tokens=10,
            output_tokens=5,
        )

    monkeypatch.setattr("agentforge.eval.solve_task", fake_solve)
    cfg = ModelConfig(base_url="test", api_key="", model="fake")
    report = run_benchmark(cfg, tasks, num_trials=5, k=5, out_dir=tmp_path / "report")
    assert report["summary"]["pass@1"] == 0.5
    assert report["summary"]["pass@k"] == 0.5
    assert report["summary"]["pass@3"] == 0.5
    assert report["summary"]["pass@5"] == 0.5
    assert report["task_order"] == ["pass", "fail"]
    loaded = load_report(tmp_path / "report" / "report.json")
    assert loaded["episodes"] == json.loads((tmp_path / "report" / "episodes.json").read_text(encoding="utf-8"))
    assert loaded["config"]["api_key"] == "<redacted>" or loaded["config"]["api_key"] == ""

    (tmp_path / "report" / "report.json").write_text("{corrupt", encoding="utf-8")
    recovered = run_benchmark(cfg, tasks[:1], num_trials=1, k=1, out_dir=tmp_path / "report")
    assert load_report(tmp_path / "report" / "report.json")["task_order"] == ["pass"]
    assert recovered["seed"] == 0
