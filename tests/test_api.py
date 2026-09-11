"""FastAPI lifecycle smoke test using the same runtime service as production."""

from __future__ import annotations

import time
from pathlib import Path

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
        config = client.get("/config")
        assert config.status_code == 200
        assert config.json()["sandbox"]["allowed_backends"] == ["local", "docker", "podman", "auto"]
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


def test_api_workbench_detail_supports_verify_metrics_diff_and_export(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()

    class FakeAgent:
        def __init__(self, workdir):
            self.workdir = Path(workdir)
            self.calls = 0
            self.memory = type("Memory", (), {"steps": []})()
            self.model = type("Model", (), {"compressions": []})()
            self.tools = {}

        def run(self, prompt, reset=True, max_steps=20):
            self.calls += 1
            if self.calls == 2:
                (self.workdir / "verified.txt").write_text("ok\n", encoding="utf-8")
            return f"answer {self.calls}"

    monkeypatch.setattr("agentforge.harness.make_agent", lambda cfg, workdir, **kwargs: FakeAgent(workdir))
    cfg = ModelConfig(
        base_url="http://fake",
        api_key="",
        model="configured-model",
        sandbox_backend="local",
        sandbox_disk_limit_mb=0,
        state_db=str(tmp_path / "state.sqlite3"),
        trace_dir=str(tmp_path / "traces"),
    )
    service = RunService(cfg)
    app = create_app(service=service)
    command = 'python -c "from pathlib import Path; raise SystemExit(0 if Path(\'verified.txt\').exists() else 1)"'
    with TestClient(app) as client:
        created = client.post(
            "/runs",
            json={
                "repo": str(repo),
                "task": "verified task",
                "run_id": "workbench-run",
                "model": "request-model",
                "sandbox_backend": "local",
                "max_steps": 7,
                "verify_command": command,
                "verify_attempts": 2,
            },
        )
        assert created.status_code == 202
        deadline = time.time() + 3
        while time.time() < deadline:
            loaded = client.get("/runs/workbench-run").json()
            if loaded["status"] == "succeeded":
                break
            time.sleep(0.02)
        assert loaded["status"] == "succeeded"
        detail = client.get("/runs/workbench-run/trace")
        assert detail.status_code == 200
        body = detail.json()
        assert body["config"]["model"] == "request-model"
        assert body["config"]["sandbox_backend"] == "local"
        assert body["metrics"]["verification_count"] == 2
        assert body["metrics"]["verification_passed"] == 1
        assert body["checkpoint"]["exists"] is True
        assert body["verifications"][0]["reward"] == 0
        assert body["verifications"][1]["reward"] == 1
        assert client.get("/runs/workbench-run/summary").json()["metrics"]["step_count"] >= 0
        exported = client.get("/runs/workbench-run/export")
        assert exported.status_code == 200
        assert "attachment" in exported.headers["content-disposition"]


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


def test_api_benchmark_records_survive_service_rebuild(tmp_path, monkeypatch):
    cfg = ModelConfig(
        base_url="http://localhost",
        api_key="",
        model="test",
        state_db=str(tmp_path / "state.sqlite3"),
        trace_dir=str(tmp_path / "traces"),
    )

    def fake_benchmark(cfg, tasks, *, num_trials, k, out_dir, seed):
        if seed == 99:
            raise RuntimeError("benchmark provider unavailable")
        return {"report_path": str(tmp_path / "completed" / "report.json")}

    monkeypatch.setattr("agentforge.benchmark.run_benchmark", fake_benchmark)
    service = RunService(cfg)
    app = create_app(service=service)
    with TestClient(app) as client:
        completed = client.post(
            "/benchmarks",
            json={"tasks": ["fix-add"], "trials": 1, "k": 1, "seed": 1},
        ).json()
        failed = client.post(
            "/benchmarks",
            json={"tasks": ["fix-add"], "trials": 1, "k": 1, "seed": 99},
        ).json()
        deadline = time.time() + 3
        while time.time() < deadline:
            completed_state = client.get(f"/benchmarks/{completed['id']}").json()
            failed_state = client.get(f"/benchmarks/{failed['id']}").json()
            if completed_state["status"] == "succeeded" and failed_state["status"] == "failed":
                break
            time.sleep(0.02)
        assert completed_state["status"] == "succeeded"
        report_path = Path(completed_state["report_path"])
        assert report_path.parent.name == "completed"
        assert report_path.name == "report.json"
        assert failed_state["status"] == "failed"
        assert "provider unavailable" in failed_state["error"]

    rebuilt = RunService(cfg)
    try:
        assert rebuilt.get_benchmark(completed["id"])["status"] == "succeeded"
        assert rebuilt.get_benchmark(failed["id"])["status"] == "failed"
    finally:
        rebuilt.close()


def test_api_run_state_survives_service_rebuild(tmp_path):
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

    def fail(_ctx):
        raise RuntimeError("persisted failure")

    service.runtime.run("persistent-run", str(repo), fail, run_id="persistent-run")
    service.close()

    rebuilt = RunService(cfg)
    app = create_app(service=rebuilt)
    with TestClient(app) as client:
        loaded = client.get("/runs/persistent-run")
        assert loaded.status_code == 200
        assert loaded.json()["status"] == "failed"
        assert "persisted failure" in loaded.json()["error"]


def test_api_runs_real_worker_with_fake_provider_and_records_trace_metrics(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()

    class FakeAgent:
        def __init__(self):
            self.memory = type("Memory", (), {"steps": []})()
            self.model = type("Model", (), {"compressions": []})()
            self.tools = {}

        def run(self, task, reset=True, max_steps=20):
            return f"done:{task}:{reset}:{max_steps}"

    monkeypatch.setattr("agentforge.harness.make_agent", lambda *args, **kwargs: FakeAgent())
    cfg = ModelConfig(
        base_url="http://fake",
        api_key="",
        model="fake",
        state_db=str(tmp_path / "state.sqlite3"),
        trace_dir=str(tmp_path / "traces"),
    )
    service = RunService(cfg)
    app = create_app(service=service)
    with TestClient(app) as client:
        created = client.post("/runs", json={"repo": str(repo), "task": "real-worker", "run_id": "e2e"})
        assert created.status_code == 202
        deadline = time.time() + 3
        while time.time() < deadline:
            loaded = client.get("/runs/e2e").json()
            if loaded["status"] == "succeeded":
                break
            time.sleep(0.02)
        assert loaded["status"] == "succeeded"
        assert loaded["trace_id"]
        trace = client.get("/runs/e2e/trace").json()
        trace_events = trace["events"]
        assert any(event["type"] == "run.trace_started" for event in trace_events)
        assert all(
            event["payload"].get("trace_id") == loaded["trace_id"]
            for event in trace_events
            if event["type"] != "run.created"
        )
        metrics = ""
        deadline = time.time() + 1
        while time.time() < deadline:
            metrics = client.get("/metrics").text
            if "agentforge_run_duration_seconds_bucket" in metrics:
                break
            time.sleep(0.01)
        assert "agentforge_run_duration_seconds_bucket" in metrics


def test_api_real_worker_failure_and_cancel_are_persisted(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()

    class FakeAgent:
        def __init__(self):
            self.memory = type("Memory", (), {"steps": []})()
            self.model = type("Model", (), {"compressions": []})()
            self.tools = {}

        def run(self, task, reset=True, max_steps=20):
            if task == "fail":
                raise RuntimeError("fake provider failure")
            time.sleep(0.5)
            return "cancelled later"

    monkeypatch.setattr("agentforge.harness.make_agent", lambda *args, **kwargs: FakeAgent())
    cfg = ModelConfig(
        base_url="http://fake",
        api_key="",
        model="fake",
        state_db=str(tmp_path / "state.sqlite3"),
        trace_dir=str(tmp_path / "traces"),
    )
    service = RunService(cfg)
    app = create_app(service=service)
    with TestClient(app) as client:
        failed = client.post("/runs", json={"repo": str(repo), "task": "fail", "run_id": "api-fail"})
        assert failed.status_code == 202
        deadline = time.time() + 3
        while time.time() < deadline:
            failed_state = client.get("/runs/api-fail").json()
            if failed_state["status"] == "failed":
                break
            time.sleep(0.02)
        assert failed_state["status"] == "failed"
        assert "fake provider failure" in failed_state["error"]

        cancelled = client.post(
            "/runs",
            json={"repo": str(repo), "task": "cancel", "run_id": "api-cancel"},
        )
        assert cancelled.status_code == 202
        deadline = time.time() + 1
        while time.time() < deadline:
            current = client.get("/runs/api-cancel").json()
            if current["status"] == "running":
                break
            time.sleep(0.01)
        assert client.post("/runs/api-cancel/cancel").json()["status"] == "cancelled"
        assert client.get("/runs/api-cancel/trace").status_code == 200


def test_resume_api_returns_new_attempt_and_completes_with_fake_provider(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()

    class FakeAgent:
        def __init__(self):
            self.memory = type("Memory", (), {"steps": []})()
            self.model = type("Model", (), {"compressions": []})()
            self.tools = {}

        def run(self, task, reset=True, max_steps=20):
            time.sleep(0.2)
            return "resumed"

    monkeypatch.setattr("agentforge.harness.make_agent", lambda *args, **kwargs: FakeAgent())
    cfg = ModelConfig(
        base_url="http://fake",
        api_key="",
        model="fake",
        state_db=str(tmp_path / "state.sqlite3"),
        trace_dir=str(tmp_path / "traces"),
    )
    service = RunService(cfg)

    def fail(_ctx):
        _ctx.checkpoint({"cursor": 1}, complete=True)
        raise RuntimeError("interrupted")

    service.runtime.run("resume-me", str(repo), fail, run_id="resume-me")
    app = create_app(service=service)
    with TestClient(app) as client:
        resumed = client.post("/runs/resume-me/resume")
        assert resumed.status_code == 202
        assert resumed.json()["attempt_id"]
        assert resumed.json()["checkpoint_sequence"] == 1
        conflict = client.post("/runs/resume-me/resume")
        assert conflict.status_code == 409
        assert "resume in progress" in conflict.json()["detail"]
        deadline = time.time() + 3
        while time.time() < deadline:
            loaded = client.get("/runs/resume-me").json()
            if loaded["status"] == "succeeded":
                break
            time.sleep(0.02)
        assert loaded["status"] == "succeeded"
        trace = client.get("/runs/resume-me/trace").json()
        assert len(trace["attempts"]) == 2
        started = [event for event in trace["events"] if event["type"] == "run.started"]
        assert started[-1]["payload"]["resume"] is True
        assert started[-1]["payload"]["checkpoint_sequence"] == 1
