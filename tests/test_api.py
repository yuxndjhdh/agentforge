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
