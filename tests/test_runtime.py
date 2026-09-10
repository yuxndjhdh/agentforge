"""Independent runtime persistence and lifecycle tests."""

from __future__ import annotations

import threading
import time

from agentforge.runtime import AgentRuntime, RunStatus, RuntimeStore


def _runtime(tmp_path):
    store = RuntimeStore(tmp_path / "state.sqlite3", tmp_path / "events")
    return AgentRuntime(store, trace_root=tmp_path / "traces"), store


def test_run_persists_related_objects_and_events(tmp_path):
    runtime, store = _runtime(tmp_path)
    try:
        repo = tmp_path / "repo"
        repo.mkdir()

        def execute(ctx):
            ctx.step("action", number=1, input={"task": "x"}, output="ok")
            ctx.verify(reward=1, checks=[{"kind": "command", "returncode": 0}])
            return "finished"

        result = runtime.run("do work", str(repo), execute)
        loaded = store.get_run(result.run.id)
        assert loaded is not None and loaded.status == RunStatus.SUCCEEDED
        assert store.list_attempts(result.run.id)[0].id == result.attempt.id
        assert store.list_steps(result.run.id)[0].attempt_id == result.attempt.id
        assert store.list_verifications(result.run.id)[0].reward == 1
        events = store.events.read(result.run.id)
        assert events and events[-1]["type"] == "run.completed"
        assert [event["sequence"] for event in events] == list(range(1, len(events) + 1))
        assert result.trace_path.exists()
    finally:
        store.close()


def test_repeated_run_id_is_idempotent(tmp_path):
    runtime, store = _runtime(tmp_path)
    try:
        repo = tmp_path / "repo"
        repo.mkdir()
        calls = []

        def execute(ctx):
            calls.append(1)
            return "once"

        first = runtime.run("task", str(repo), execute, run_id="run_fixed")
        second = runtime.run("task", str(repo), lambda _: calls.append(2), run_id="run_fixed")
        assert first.answer == second.answer == "once"
        assert calls == [1]
        assert len(store.list_attempts("run_fixed")) == 1
    finally:
        store.close()


def test_failed_run_can_resume_from_last_checkpoint(tmp_path):
    runtime, store = _runtime(tmp_path)
    try:
        repo = tmp_path / "repo"
        repo.mkdir()

        def fail(ctx):
            ctx.step("action", number=1, output="before crash")
            ctx.checkpoint({"cursor": 1, "trace_steps": ctx.trace_steps}, complete=True)
            raise RuntimeError("simulated interruption")

        first = runtime.run("task", str(repo), fail, run_id="recoverable")
        assert first.run.status == RunStatus.FAILED
        checkpoint = store.latest_checkpoint("recoverable")
        assert checkpoint is not None and checkpoint.state["cursor"] == 1

        second = runtime.resume("recoverable", lambda ctx: "resumed")
        assert second.run.status == RunStatus.SUCCEEDED
        assert len(store.list_attempts("recoverable")) == 2
    finally:
        store.close()


def test_cancelled_run_reclaims_execution(tmp_path):
    runtime, store = _runtime(tmp_path)
    try:
        repo = tmp_path / "repo"
        repo.mkdir()
        started = threading.Event()

        def execute(ctx):
            started.set()
            while True:
                ctx.check_cancelled()
                time.sleep(0.005)

        holder = {}

        def target():
            holder["result"] = runtime.run("task", str(repo), execute, run_id="cancel-me")

        thread = threading.Thread(target=target)
        thread.start()
        assert started.wait(2)
        runtime.cancel("cancel-me")
        thread.join(2)
        assert not thread.is_alive()
        assert holder["result"].run.status == RunStatus.CANCELLED
    finally:
        store.close()


def test_cancel_does_not_rewrite_terminal_run(tmp_path):
    runtime, store = _runtime(tmp_path)
    try:
        repo = tmp_path / "repo"
        repo.mkdir()
        result = runtime.run("task", str(repo), lambda ctx: "done", run_id="terminal")
        cancelled = runtime.cancel("terminal")
        assert cancelled is not None
        assert cancelled.status == RunStatus.SUCCEEDED
        assert store.get_run(result.run.id).status == RunStatus.SUCCEEDED
    finally:
        store.close()
