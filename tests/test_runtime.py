"""Independent runtime persistence and lifecycle tests."""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from agentforge.runtime import (
    AgentRuntime,
    CheckpointSchemaError,
    RunCancelled,
    RunStatus,
    RunTimedOut,
    RuntimeStore,
)
from agentforge.runtime.models import Attempt, AttemptStatus, Checkpoint, Run


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


def test_resume_restores_state_after_store_rebuild(tmp_path):
    runtime, store = _runtime(tmp_path)
    repo = tmp_path / "repo"
    repo.mkdir()

    def fail(ctx):
        ctx.step("action", number=1, output="before crash")
        ctx.checkpoint({"cursor": 2, "business": {"written": True}}, complete=True)
        raise RuntimeError("simulated interruption")

    first = runtime.run("task", str(repo), fail, run_id="rebuild-resume")
    assert first.run.status == RunStatus.FAILED
    store.close()

    rebuilt_store = RuntimeStore(tmp_path / "state.sqlite3", tmp_path / "events")
    rebuilt_runtime = AgentRuntime(rebuilt_store, trace_root=tmp_path / "traces")
    try:
        observed = {}

        def resume(ctx):
            observed["state"] = dict(ctx.state)
            observed["restored"] = dict(ctx.restored_state)
            ctx.step("action", number=3, output="after resume")
            return "resumed"

        second = rebuilt_runtime.resume("rebuild-resume", resume)
        assert second.run.status == RunStatus.SUCCEEDED
        assert observed["state"]["cursor"] == 2
        assert observed["restored"]["business"] == {"written": True}
        assert len(rebuilt_store.list_attempts("rebuild-resume")) == 2
    finally:
        rebuilt_store.close()


def test_checkpoint_corruption_falls_back_but_schema_mismatch_is_explicit(tmp_path):
    runtime, store = _runtime(tmp_path)
    repo = tmp_path / "repo"
    repo.mkdir()
    run = runtime.create_run("task", str(repo), run_id="checkpoint-fallback")
    store.save_checkpoint(Checkpoint.create(run.id, None, 1, {"cursor": 1}, complete=True))
    store.save_checkpoint(Checkpoint.create(run.id, None, 2, {"cursor": 2}, complete=True))
    store._connection.execute(
        "UPDATE checkpoints SET state_json = ? WHERE run_id = ? AND sequence = ?",
        ("{not-json", run.id, 2),
    )
    checkpoint = store.latest_checkpoint(run.id)
    assert checkpoint is not None and checkpoint.sequence == 1 and checkpoint.state["cursor"] == 1

    store._connection.execute(
        "UPDATE checkpoints SET schema_version = ? WHERE run_id = ? AND sequence = ?",
        (99, run.id, 1),
    )
    with pytest.raises(CheckpointSchemaError, match="unsupported checkpoint schema"):
        store.latest_checkpoint(run.id)
    store.close()


def test_event_and_checkpoint_sequences_are_independent_and_jsonl_tail_is_tolerated(tmp_path):
    runtime, store = _runtime(tmp_path)
    try:
        repo = tmp_path / "repo"
        repo.mkdir()
        run = runtime.create_run("task", str(repo), run_id="separate-sequences")
        event = runtime.emit(run.id, "custom")
        checkpoint = store.save_checkpoint(Checkpoint.create(run.id, None, 1, {"ok": True}))
        assert event["sequence"] == 2
        assert checkpoint.sequence == 1
        event_path = Path(store.events.path(run.id))
        with event_path.open("a", encoding="utf-8") as stream:
            stream.write('{"partial":')
        assert [item["sequence"] for item in store.events.read(run.id)] == [1, 2]
    finally:
        store.close()


def test_successful_tool_call_is_replayed_across_attempts(tmp_path):
    runtime, store = _runtime(tmp_path)
    try:
        repo = tmp_path / "repo"
        repo.mkdir()
        side_effects = []

        def first(ctx):
            result = ctx.call_tool(
                "write_file",
                lambda: side_effects.append("write") or "wrote",
                idempotency_key="repo-file-write",
            )
            assert result == "wrote"
            ctx.checkpoint({"cursor": 1}, complete=True)
            raise RuntimeError("crash after side effect")

        runtime.run("task", str(repo), first, run_id="idempotent-tool")

        def resumed(ctx):
            result = ctx.call_tool(
                "write_file",
                lambda: side_effects.append("duplicate") or "wrong",
                idempotency_key="repo-file-write",
            )
            assert result == "wrote"
            return "resumed"

        result = runtime.resume("idempotent-tool", resumed)
        assert result.run.status == RunStatus.SUCCEEDED
        assert side_effects == ["write"]
        assert len(store.list_tool_calls("idempotent-tool")) == 1
    finally:
        store.close()


def test_failed_tool_requires_explicit_retry_policy(tmp_path):
    runtime, store = _runtime(tmp_path)
    try:
        repo = tmp_path / "repo"
        repo.mkdir()

        def fail_once(ctx):
            ctx.call_tool(
                "publish",
                lambda: (_ for _ in ()).throw(RuntimeError("temporary failure")),
                idempotency_key="publish-artifact",
            )
            return "unreachable"

        first = runtime.run("task", str(repo), fail_once, run_id="retry-policy")
        assert first.run.status == RunStatus.FAILED

        def refused(ctx):
            ctx.call_tool("publish", lambda: "should not run", idempotency_key="publish-artifact")
            return "unreachable"

        second = runtime.resume("retry-policy", refused)
        assert second.run.status == RunStatus.FAILED
        assert isinstance(second.run.error, str) and "explicit retry" in second.run.error

        calls = []

        def retry(ctx):
            value = ctx.call_tool(
                "publish",
                lambda: calls.append("retried") or "published",
                idempotency_key="publish-artifact",
                retry_failed=True,
            )
            assert value == "published"
            return "done"

        third = runtime.resume("retry-policy", retry)
        assert third.run.status == RunStatus.SUCCEEDED
        assert calls == ["retried"]
        assert len(store.list_tool_calls("retry-policy")) == 2
    finally:
        store.close()


def test_runtime_tool_event_policy_and_replay_are_persisted(tmp_path):
    runtime, store = _runtime(tmp_path)
    try:
        repo = tmp_path / "repo"
        repo.mkdir()

        def execute(ctx):
            runtime._handle_tool_event(ctx, {"phase": "policy", "allowed": False, "reason": "test"})
            event = {
                "phase": "started",
                "call_id": "call-1",
                "name": "write_file",
                "idempotency_key": "stable-call",
                "arguments": {"path": "a.txt"},
            }
            assert runtime._handle_tool_event(ctx, event) is None
            runtime._handle_tool_event(
                ctx,
                {
                    "phase": "completed",
                    "call_id": "call-1",
                    "observation": "wrote",
                },
            )
            replay = dict(event)
            replay["call_id"] = "call-2"
            decision = runtime._handle_tool_event(ctx, replay)
            assert decision == {"replay": True, "observation": "wrote"}
            return "done"

        result = runtime.run("task", str(repo), execute)
        assert result.run.status == RunStatus.SUCCEEDED
        events = store.events.read(result.run.id)
        assert any(item["type"] == "policy.decision" for item in events)
        assert any(item["type"] == "tool_call.replayed" for item in events)
        assert len(store.list_tool_calls(result.run.id)) == 1
    finally:
        store.close()


def test_context_rejects_closed_cancelled_and_timed_out_operations(tmp_path):
    runtime, store = _runtime(tmp_path)
    try:
        repo = tmp_path / "repo"
        repo.mkdir()
        captured = {}

        def execute(ctx):
            ctx.closed = True
            with pytest.raises(RunCancelled, match="closed"):
                ctx.check_cancelled()
            ctx.closed = False
            ctx.timed_out = True
            with pytest.raises(RunTimedOut):
                ctx.check_cancelled()
            ctx.timed_out = False
            ctx.cancel_event.set()
            with pytest.raises(RunCancelled, match="cancelled"):
                ctx.check_cancelled()
            ctx.cancel_event.clear()
            captured["state"] = ctx.state
            return "done"

        result = runtime.run("task", str(repo), execute)
        assert result.run.status == RunStatus.SUCCEEDED
        assert captured["state"] == {}
    finally:
        store.close()


def test_run_agent_adapter_persists_only_current_memory_and_rebuilds_trace(tmp_path):
    runtime, store = _runtime(tmp_path)
    try:
        repo = tmp_path / "repo"
        repo.mkdir()

        class TokenUsage:
            input_tokens = 3
            output_tokens = 2

        class ToolCall:
            name = "read_file"
            arguments = {"path": "a.py"}

        class ActionStep:
            step_number = 1
            tool_calls = [ToolCall()]
            observations = "ok"
            token_usage = TokenUsage()

        class FakeAgent:
            def __init__(self):
                self.memory = type("Memory", (), {"steps": []})()
                self.model = type("Model", (), {"compressions": []})()
                self.tools = {}

            def run(self, task, reset=True, max_steps=20):
                self.memory.steps.append(ActionStep())
                return f"answer:{task}:{reset}:{max_steps}"

        cfg = type("Config", (), {"model": "fake", "max_steps": 4})()
        first = runtime.run_agent(cfg, "task", str(repo), agent=FakeAgent(), run_id="agent-adapter")
        assert first.run.status == RunStatus.SUCCEEDED
        assert store.list_steps("agent-adapter")
        trace_path = first.trace_path
        trace_path.unlink()
        second = runtime.run_agent(cfg, "task", str(repo), agent=FakeAgent(), run_id="agent-adapter")
        assert second.answer == first.answer
        assert second.trace_path.exists()
    finally:
        store.close()


def test_run_agent_independent_verification_retries_with_feedback(tmp_path):
    runtime, store = _runtime(tmp_path)
    try:
        repo = tmp_path / "repo"
        repo.mkdir()

        class FakeAgent:
            def __init__(self):
                self.memory = type("Memory", (), {"steps": []})()
                self.model = type("Model", (), {"compressions": []})()
                self.tools = {}
                self.calls = []

            def run(self, prompt, reset=True, max_steps=20):
                self.calls.append({"prompt": prompt, "reset": reset, "max_steps": max_steps})
                if len(self.calls) == 2:
                    (repo / "verified.txt").write_text("ok\n", encoding="utf-8")
                return "agent answer"

        cfg = type(
            "Config",
            (),
            {
                "model": "fake",
                "max_steps": 4,
                "sandbox_backend": "local",
                "sandbox_timeout": 5.0,
                "sandbox_max_output_bytes": 4096,
                "sandbox_readonly": False,
                "sandbox_network": False,
                "sandbox_cpu_limit": 1.0,
                "sandbox_memory_limit_mb": 64,
                "sandbox_pids_limit": 16,
                "sandbox_disk_limit_mb": 0,
                "sandbox_image": "python:3.13-slim@sha256:9d2e5553305c7c7b0097999bb17187c69b921ccd6bc9d40e4bb5ebe652c00285",
                "sandbox_whitelist": "",
                "sandbox_denylist": "",
                "sandbox_deny_patterns": "",
            },
        )()
        agent = FakeAgent()
        result = runtime.run_agent(
            cfg,
            "make verified change",
            str(repo),
            agent=agent,
            verify_command='python -c "from pathlib import Path; raise SystemExit(0 if Path(\'verified.txt\').exists() else 1)"',
            verify_attempts=2,
        )

        assert result.run.status == RunStatus.SUCCEEDED
        assert len(agent.calls) == 2
        assert agent.calls[0]["reset"] is True
        assert agent.calls[1]["reset"] is False
        assert "独立验收未通过" in agent.calls[1]["prompt"]
        verifications = store.list_verifications(result.run.id)
        assert [item.reward for item in verifications] == [0, 1]
        assert all(item.checks[0]["kind"] == "command" for item in verifications)
        assert any(event["type"] == "run.verifying" for event in store.events.read(result.run.id))
    finally:
        store.close()


def test_recover_stale_run_closes_attempt_and_records_event(tmp_path):
    store = RuntimeStore(tmp_path / "state.sqlite3", tmp_path / "events")
    repo = tmp_path / "repo"
    repo.mkdir()
    run = Run.create("stale", str(repo))
    store.create_run(run)
    attempt = Attempt.create(run.id, 0, status=AttemptStatus.RUNNING)
    store.create_attempt(attempt)
    store.update_run(run.id, status=RunStatus.RUNNING)
    checkpoint = Checkpoint.create(run.id, attempt.id, 1, {"cursor": 2}, complete=True)
    store.save_checkpoint(checkpoint)
    store.close()

    rebuilt = RuntimeStore(tmp_path / "state.sqlite3", tmp_path / "events")
    try:
        recovered = rebuilt.recover_stale_runs()
        assert [item.id for item in recovered] == [run.id]
        loaded = rebuilt.get_run(run.id)
        assert loaded is not None
        assert loaded.status == RunStatus.FAILED
        assert loaded.metadata["resumable"] is True
        assert rebuilt.get_attempt(attempt.id).status == AttemptStatus.FAILED
        events = rebuilt.events.read(run.id)
        assert events[-1]["type"] == "run.recovered"
        assert events[-1]["payload"]["checkpoint_sequence"] == 1
    finally:
        rebuilt.close()


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


def test_cancel_non_cooperative_executor_returns_without_polling(tmp_path):
    runtime, store = _runtime(tmp_path)
    try:
        repo = tmp_path / "repo"
        repo.mkdir()
        started = threading.Event()
        release = threading.Event()
        holder = {}

        def execute(_ctx):
            started.set()
            while not release.is_set():
                time.sleep(0.01)
            return "late result"

        thread = threading.Thread(
            target=lambda: holder.setdefault(
                "result", runtime.run("task", str(repo), execute, run_id="non-cooperative")
            )
        )
        thread.start()
        assert started.wait(2)
        runtime.cancel("non-cooperative")
        thread.join(1)
        assert not thread.is_alive()
        assert holder["result"].run.status == RunStatus.CANCELLED
        release.set()
    finally:
        store.close()


def test_total_timeout_returns_for_blocking_executor(tmp_path):
    runtime, store = _runtime(tmp_path)
    try:
        repo = tmp_path / "repo"
        repo.mkdir()
        release = threading.Event()
        holder = {}

        def execute(_ctx):
            while not release.is_set():
                time.sleep(0.01)
            return "late result"

        started = time.monotonic()
        thread = threading.Thread(
            target=lambda: holder.setdefault(
                "result",
                runtime.run("task", str(repo), execute, run_id="timed-out", max_duration=0.1),
            )
        )
        thread.start()
        thread.join(1)
        assert not thread.is_alive()
        assert holder["result"].run.status == RunStatus.FAILED
        assert "max duration" in (holder["result"].run.error or "")
        assert time.monotonic() - started < 1.5
        release.set()
    finally:
        store.close()


def test_agent_request_timeout_sets_interrupt_switch(tmp_path):
    runtime, store = _runtime(tmp_path)
    try:
        repo = tmp_path / "repo"
        repo.mkdir()
        started = threading.Event()

        class SlowAgent:
            def __init__(self):
                self.interrupt_switch = False
                self.memory = type("Memory", (), {"steps": []})()
                self.model = type("Model", (), {"compressions": []})()
                self.tools = {}

            def run(self, task, reset=True, max_steps=20):
                started.set()
                while not self.interrupt_switch:
                    time.sleep(0.01)
                return "late model answer"

        cfg = type("Config", (), {"model": "fake", "max_steps": 2})()
        agent = SlowAgent()
        holder = {}
        thread = threading.Thread(
            target=lambda: holder.setdefault(
                "result",
                runtime.run_agent(
                    cfg,
                    "slow model",
                    str(repo),
                    agent=agent,
                    run_id="slow-model",
                    max_duration=0.1,
                ),
            )
        )
        thread.start()
        assert started.wait(2)
        thread.join(1)
        assert not thread.is_alive()
        assert agent.interrupt_switch is True
        assert holder["result"].run.status == RunStatus.FAILED
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
