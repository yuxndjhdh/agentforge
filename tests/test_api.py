"""FastAPI lifecycle smoke test using the same runtime service as production."""

from __future__ import annotations

from fastapi.testclient import TestClient

from agentforge.api import create_app
from agentforge.api.app import RunService
from agentforge.config import ModelConfig


def test_run_api_lifecycle(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    cfg = ModelConfig(
        base_url="http://localhost",
        api_key="",
        model="test",
        state_db=str(tmp_path / "state.sqlite3"),
        trace_dir=str(tmp_path / "traces"),
    )
    service = RunService(cfg)
    monkeypatch.setattr(service, "_run", lambda run_id, request, workdir: None)
    app = create_app(service=service)

    with TestClient(app) as client:
        assert client.get("/").status_code == 200
        created = client.post(
            "/runs",
            json={"repo": str(repo), "task": "noop", "run_id": "api-test"},
        )
        assert created.status_code == 202
        assert created.json()["id"] == "api-test"
        assert client.get("/runs/api-test").status_code == 200
        trace = client.get("/runs/api-test/trace")
        assert trace.status_code == 200
        assert trace.json()["run"]["id"] == "api-test"
        cancelled = client.post("/runs/api-test/cancel")
        assert cancelled.status_code == 200
        assert cancelled.json()["status"] == "cancelled"
        assert client.get("/metrics").status_code == 200


def test_api_benchmark_uses_full_registry_and_rejects_unknown(tmp_path, monkeypatch):
    cfg = ModelConfig(
        base_url="http://localhost",
        api_key="",
        model="test",
        state_db=str(tmp_path / "state.sqlite3"),
        trace_dir=str(tmp_path / "traces"),
    )

    def fake_benchmark(cfg, tasks, *, num_trials, k, out_dir, seed):
        return {
            "summary": {"tasks": len(tasks), "episodes": len(tasks) * num_trials, "pass@1": 0.0, "pass@k": 0.0},
            "task_order": [task.name for task in tasks],
            "seed": seed,
        }

    monkeypatch.setattr("agentforge.benchmark.run_benchmark", fake_benchmark)
    service = RunService(cfg)
    app = create_app(service=service)
    with TestClient(app) as client:
        created = client.post("/benchmarks", json={"trials": 2, "k": 2, "seed": 7})
        assert created.status_code == 202
        assert len(created.json()["tasks"]) == 23
        assert created.json()["seed"] == 7
        unknown = client.post("/benchmarks", json={"tasks": ["missing-task"]})
        assert unknown.status_code == 400
        assert "unknown benchmark task" in unknown.json()["detail"]
        invalid_k = client.post("/benchmarks", json={"trials": 1, "k": 2})
        assert invalid_k.status_code == 400
        assert "k must be <= trials" in invalid_k.json()["detail"]
