from __future__ import annotations

import os
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from agentforge.api import create_app
from agentforge.api.app import RunService
from agentforge.benchmark import BenchmarkOptions, run_benchmark
from agentforge.config import ModelConfig, load_config
from agentforge.eval import Check, CodeTask, EvalResult, solve_task
from agentforge.trace import RunTrace


def _task(name: str = "variant-task") -> CodeTask:
    def seed(workdir: str) -> None:
        with open(os.path.join(workdir, "result.txt"), "w", encoding="utf-8") as stream:
            stream.write("bad")

    return CodeTask(
        name=name,
        instruction="fix result",
        build_seed=seed,
        checks=[Check(kind="file_contains", path="result.txt", needles=["good"])],
        source="test",
        task_version="variant-1",
    )


class RetryAgent:
    def __init__(self, workdir: str):
        self.workdir = workdir
        self.calls: list[dict] = []
        self.memory = SimpleNamespace(steps=[])
        self.model = SimpleNamespace(
            context_compression_enabled=False,
            compressions=[],
            compression_stats=[{"status": "disabled", "compressed": False}],
        )

    def run(self, prompt: str, reset: bool = True) -> str:
        self.calls.append({"prompt": prompt, "reset": reset})
        if len(self.calls) == 2:
            with open(os.path.join(self.workdir, "result.txt"), "w", encoding="utf-8") as stream:
                stream.write("good")
        return f"attempt-{len(self.calls)}"


def test_context_compression_environment_switch(monkeypatch):
    monkeypatch.setenv("HARNESS_CONTEXT_COMPRESSION", "0")
    assert load_config().context_compression_enabled is False
    monkeypatch.setenv("HARNESS_CONTEXT_COMPRESSION", "1")
    assert load_config().context_compression_enabled is True
    monkeypatch.setenv("HARNESS_CONTEXT_COMPRESSION", "invalid")
    with pytest.raises(ValueError, match="HARNESS_CONTEXT_COMPRESSION"):
        load_config()


def test_verify_retry_is_shared_with_eval_episode_executor(tmp_path):
    task = _task()
    task.build_seed(str(tmp_path))
    agent = RetryAgent(str(tmp_path))

    one_shot = solve_task(
        ModelConfig(base_url="", api_key="", model="fake"),
        task,
        workdir=str(tmp_path),
        agent=RetryAgent(str(tmp_path)),
        verify_enabled=False,
    )
    assert one_shot.reward == 0
    assert len(one_shot.attempts) == 1

    task.build_seed(str(tmp_path))
    result = solve_task(
        ModelConfig(base_url="", api_key="", model="fake"),
        task,
        workdir=str(tmp_path),
        agent=agent,
        verify_enabled=True,
        max_attempts=3,
        out_dir=tmp_path / "traces",
    )
    assert result.reward == 1
    assert len(result.attempts) == 2
    assert result.attempts[0]["reward"] == 0
    assert result.attempts[1]["reward"] == 1
    assert result.attempts[1]["trace_path"] == str(result.trace_path)
    assert agent.calls[0]["reset"] is True
    assert agent.calls[1]["reset"] is False
    assert "自检未通过" in agent.calls[1]["prompt"]
    assert len([step for step in result.trace.steps if step.get("kind") == "verify"]) == 2


def test_benchmark_options_map_to_fixed_experiments():
    assert BenchmarkOptions(False, False, 1).experiment_id == "A"
    assert BenchmarkOptions(True, False, 1).experiment_id == "B"
    assert BenchmarkOptions(False, True, 3).experiment_id == "C"
    assert BenchmarkOptions(True, True, 3).experiment_id == "D"
    with pytest.raises(ValueError, match="requires verify_retry"):
        BenchmarkOptions(False, False, 3)
    with pytest.raises(ValueError, match="requires verify_attempts"):
        BenchmarkOptions(True, True, 1)


def test_benchmark_report_records_attempts_and_variant_metadata(tmp_path, monkeypatch):
    task_pass = _task("pass")
    task_fail = _task("fail")

    def fake_solve(cfg, task, *, out_dir, verify_enabled=False, max_attempts=1, **kwargs):
        passed = task.name == "pass"
        attempts = [
            {
                "attempt": 0,
                "reward": 0,
                "feedback": "fix it",
                "answer": "first",
                "trace_steps": [],
                "elapsed_seconds": 0.1,
                "steps": 1,
                "input_tokens": 10,
                "output_tokens": 5,
                "failure_type": "state_mismatch",
            },
            {
                "attempt": 1,
                "reward": 1 if passed else 0,
                "feedback": "" if passed else "still broken",
                "answer": "second",
                "trace_steps": [],
                "elapsed_seconds": 0.2,
                "steps": 2,
                "input_tokens": 20,
                "output_tokens": 10,
                "failure_type": None if passed else "state_mismatch",
            },
        ]
        trace = RunTrace(
            run_id=f"run-{task.name}",
            workdir=str(tmp_path),
            task=task.instruction,
            compression=[{"compressed": True, "saved_chars": 10, "saved_tokens": 3}],
            compression_stats=[{"compressed": True, "dropped_steps": 1, "saved_chars": 10, "saved_tokens": 3}],
            compression_enabled=True,
        )
        return EvalResult(
            task=task.name,
            reward=1 if passed else 0,
            workdir=str(tmp_path),
            answer="second",
            trace=trace,
            trace_path=tmp_path / f"{task.name}.json",
            elapsed_seconds=0.3,
            input_tokens=30,
            output_tokens=15,
            attempts=attempts,
            compression_enabled=True,
            compression_stats=trace.compression_stats,
        )

    monkeypatch.setattr("agentforge.eval.solve_task", fake_solve)
    cfg = ModelConfig(
        base_url="test",
        api_key="secret",
        model="fake",
        input_cost_per_million=1,
        output_cost_per_million=2,
    )
    report = run_benchmark(
        cfg,
        [task_pass, task_fail],
        num_trials=1,
        k=1,
        out_dir=tmp_path / "report",
        options=BenchmarkOptions(True, True, 3),
    )
    assert report["schema_version"] == 2
    assert report["experiment_id"] == "D"
    assert report["experiment"] == {
        "experiment_id": "D",
        "context_compression_enabled": True,
        "verify_enabled": True,
        "max_attempts": 3,
    }
    assert len(report["episodes"]) == 2
    assert report["summary"]["avg_attempts"] == 2
    assert report["summary"]["first_success_rate"] == 0
    assert report["summary"]["final_success_rate"] == 0.5
    assert report["summary"]["verify_incremental_input_tokens"] == 40
    assert report["summary"]["compression_trigger_rate"] == 1
    assert report["config"]["api_key"] == "<redacted>"
    assert report["git"]["commit"]


def test_api_benchmark_uses_the_same_variant_options(tmp_path, monkeypatch):
    captured: dict = {}

    def fake_benchmark(cfg, tasks, *, num_trials, k, out_dir, seed, options):
        captured["options"] = options
        return {"report_path": str(tmp_path / "report.json")}

    monkeypatch.setattr("agentforge.benchmark.run_benchmark", fake_benchmark)
    cfg = ModelConfig(
        base_url="test",
        api_key="",
        model="fake",
        state_db=str(tmp_path / "state.sqlite3"),
        trace_dir=str(tmp_path / "traces"),
    )
    service = RunService(cfg)
    app = create_app(service=service)
    with TestClient(app) as client:
        response = client.post(
            "/benchmarks",
            json={
                "tasks": ["fix-add"],
                "trials": 1,
                "k": 1,
                "context_compression": False,
                "verify_retry": True,
                "verify_attempts": 3,
            },
        )
        assert response.status_code == 202
        assert response.json()["experiment_id"] == "C"
        assert response.json()["experiment"]["max_attempts"] == 3
    assert captured["options"].to_dict()["experiment_id"] == "C"
