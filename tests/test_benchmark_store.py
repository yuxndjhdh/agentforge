from __future__ import annotations

from agentforge.benchmark_store import BenchmarkStore


def _record():
    return {
        "id": "benchmark-test",
        "status": "running",
        "trials": 5,
        "k": 5,
        "seed": 7,
        "tasks": ["fix-add"],
        "config": {"model": "fake", "api_key": "<redacted>"},
        "experiment": {
            "experiment_id": "A",
            "context_compression_enabled": False,
            "verify_enabled": False,
            "max_attempts": 1,
        },
    }


def test_benchmark_jobs_survive_store_rebuild_and_running_jobs_are_recovered(tmp_path):
    store = BenchmarkStore(tmp_path / "state.sqlite3")
    store.create(_record())
    store.close()

    rebuilt = BenchmarkStore(tmp_path / "state.sqlite3")
    try:
        recovered = rebuilt.recover_running()
        assert recovered[0]["status"] == "failed"
        job = rebuilt.get("benchmark-test")
        assert job is not None
        assert job["tasks"] == ["fix-add"]
        assert job["config"]["api_key"] == "<redacted>"
        assert job["experiment_id"] == "A"
        assert "restart" in job["error"]
    finally:
        rebuilt.close()
