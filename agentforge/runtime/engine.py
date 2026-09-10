"""Runtime lifecycle and smolagents adapter."""

from __future__ import annotations

import inspect
import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from ..trace import RunTrace
from .models import (
    Attempt,
    AttemptStatus,
    Checkpoint,
    Run,
    RunStatus,
    Step,
    StepStatus,
    ToolCall,
    Verification,
    VerificationStatus,
)
from .store import RuntimeStore


class RunCancelled(RuntimeError):
    """Raised by a runtime callback after cancellation was requested."""


class RunTimedOut(RunCancelled):
    """Raised when the runtime deadline elapses."""


@dataclass
class RuntimeContext:
    runtime: "AgentRuntime"
    run: Run
    attempt: Attempt
    cancel_event: threading.Event
    trace_steps: list[dict[str, Any]] = field(default_factory=list)
    active_tool_calls: dict[str, ToolCall] = field(default_factory=dict)
    state: dict[str, Any] = field(default_factory=dict)
    timed_out: bool = False

    def check_cancelled(self) -> None:
        latest = self.runtime.store.get_run(self.run.id)
        if self.timed_out:
            raise RunTimedOut("run exceeded max duration")
        if self.cancel_event.is_set() or (latest and latest.cancel_requested):
            raise RunCancelled("run cancelled")

    def checkpoint(self, state: dict[str, Any], *, complete: bool = False) -> Checkpoint:
        self.check_cancelled()
        self.state = dict(state)
        sequence = self.runtime.store.next_sequence(self.run.id)
        checkpoint = Checkpoint.create(
            self.run.id,
            self.attempt.id,
            sequence,
            self.state,
            complete=complete,
        )
        self.runtime.store.save_checkpoint(checkpoint)
        self.runtime.emit(
            self.run.id,
            "checkpoint.created",
            attempt_id=self.attempt.id,
            payload={"sequence": sequence, "complete": complete},
        )
        return checkpoint

    def step(
        self,
        kind: str,
        *,
        number: int,
        input: Any = None,
        output: Any = None,
        error: str | None = None,
        status: StepStatus = StepStatus.SUCCEEDED,
        metadata: dict[str, Any] | None = None,
    ) -> Step:
        self.check_cancelled()
        step = Step.create(
            self.run.id,
            self.attempt.id,
            number,
            kind,
            input=input,
            output=output,
            error=error,
            status=status,
            finished_at=time.time(),
            metadata=metadata or {},
        )
        self.runtime.store.append_step(step)
        self.runtime.emit(
            self.run.id,
            "step.completed",
            attempt_id=self.attempt.id,
            payload=step.to_record(),
        )
        return step

    def verify(
        self,
        *,
        reward: int,
        checks: list[dict[str, Any]] | None = None,
        feedback: str = "",
    ) -> Verification:
        verification = Verification.create(
            self.run.id,
            self.attempt.id,
            status=VerificationStatus.PASSED if reward else VerificationStatus.FAILED,
            finished_at=time.time(),
            reward=int(reward),
            checks=checks or [],
            feedback=feedback,
        )
        self.runtime.store.append_verification(verification)
        self.runtime.emit(
            self.run.id,
            "verification.completed",
            attempt_id=self.attempt.id,
            payload=verification.to_record(),
        )
        return verification


@dataclass
class RuntimeResult:
    run: Run
    attempt: Attempt
    answer: str
    trace: RunTrace
    trace_path: Path
    agent: Any = None


class AgentRuntime:
    """Own run state independently from the third-party agent memory."""

    def __init__(
        self,
        store: RuntimeStore,
        *,
        trace_root: str | Path = "runs",
    ):
        self.store = store
        self.trace_root = Path(trace_root)
        self._active: dict[str, tuple[Any, threading.Event, RuntimeContext]] = {}
        self._lock = threading.RLock()

    def emit(
        self,
        run_id: str,
        event_type: str,
        *,
        attempt_id: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return self.store.append_event(run_id, event_type, attempt_id=attempt_id, payload=payload)

    def create_run(
        self,
        task: str,
        workdir: str,
        *,
        run_id: str | None = None,
        model: str = "",
        project_id: str = "default",
        user_id: str = "default",
        max_steps: int = 20,
        max_duration: float | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> Run:
        if not os.path.isdir(workdir):
            raise ValueError(f"workdir is not a directory: {workdir}")
        run = Run(
            id=run_id or Run.create(task, workdir).id,
            task=task,
            workdir=os.path.realpath(workdir),
            model=model,
            project_id=project_id,
            user_id=user_id,
            max_steps=max_steps,
            max_duration=max_duration,
            metadata=metadata or {},
        )
        self.store.create_run(run)
        self.emit(run.id, "run.created", payload=run.to_record())
        return run

    def run(
        self,
        task: str,
        workdir: str,
        executor: Callable[[RuntimeContext], Any],
        *,
        run_id: str | None = None,
        model: str = "",
        project_id: str = "default",
        user_id: str = "default",
        max_steps: int = 20,
        max_duration: float | None = None,
        resume: bool = False,
        agent: Any = None,
        trace_steps: list[dict[str, Any]] | None = None,
    ) -> RuntimeResult:
        run = self.store.get_run(run_id) if run_id else None
        if run is None:
            run = self.create_run(
                task,
                workdir,
                run_id=run_id,
                model=model,
                project_id=project_id,
                user_id=user_id,
                max_steps=max_steps,
                max_duration=max_duration,
            )
        elif run.status == RunStatus.SUCCEEDED and run.answer is not None:
            return self._result_from_stored(run)
        elif run.status == RunStatus.RUNNING:
            raise ValueError(f"run {run.id} is already running")
        elif run.status == RunStatus.CANCELLED and not resume:
            raise ValueError(f"run {run.id} is cancelled; pass resume=True to continue")

        attempts = self.store.list_attempts(run.id)
        attempt = Attempt.create(run.id, len(attempts))
        now = time.time()
        attempt.status = AttemptStatus.RUNNING
        attempt.started_at = now
        self.store.create_attempt(attempt)
        self.store.update_run(run.id, status=RunStatus.RUNNING, error=None, cancel_requested=False)
        self.emit(run.id, "run.started", attempt_id=attempt.id, payload={"resume": resume})
        self.emit(run.id, "attempt.started", attempt_id=attempt.id, payload=attempt.to_record())

        cancel_event = threading.Event()
        context = RuntimeContext(self, run, attempt, cancel_event, trace_steps=trace_steps or [])
        with self._lock:
            self._active[run.id] = (agent, cancel_event, context)
        timer: threading.Timer | None = None
        if max_duration and max_duration > 0:
            timer = threading.Timer(max_duration, self._timeout, args=(run.id,))
            timer.daemon = True
            timer.start()
        answer = ""
        final_status = RunStatus.SUCCEEDED
        error_text: str | None = None
        try:
            answer = str(executor(context))
            context.check_cancelled()
            context.checkpoint({"answer": answer, "trace_steps": context.trace_steps}, complete=True)
        except RunTimedOut as exc:
            final_status = RunStatus.FAILED
            error_text = str(exc)
        except RunCancelled as exc:
            final_status = RunStatus.CANCELLED
            error_text = str(exc)
        except Exception as exc:
            if context.timed_out:
                final_status = RunStatus.FAILED
                error_text = "run exceeded max duration"
            elif cancel_event.is_set() or (self.store.get_run(run.id) or run).cancel_requested:
                final_status = RunStatus.CANCELLED
                error_text = "run cancelled"
            else:
                final_status = RunStatus.FAILED
                error_text = f"{type(exc).__name__}: {exc}"
        finally:
            if timer:
                timer.cancel()
            with self._lock:
                self._active.pop(run.id, None)
            finished = time.time()
            attempt.status = {
                RunStatus.SUCCEEDED: AttemptStatus.SUCCEEDED,
                RunStatus.CANCELLED: AttemptStatus.CANCELLED,
                RunStatus.FAILED: AttemptStatus.FAILED,
            }[final_status]
            attempt.finished_at = finished
            attempt.answer = answer or None
            attempt.error = error_text
            self.store.update_attempt(
                attempt.id,
                status=attempt.status,
                finished_at=finished,
                answer=attempt.answer,
                error=attempt.error,
            )
            self.store.update_run(
                run.id,
                status=final_status,
                answer=answer or None,
                error=error_text,
                cancel_requested=(final_status == RunStatus.CANCELLED),
            )
            self.emit(
                run.id,
                "run.cancelled" if final_status == RunStatus.CANCELLED else "run.completed" if final_status == RunStatus.SUCCEEDED else "run.failed",
                attempt_id=attempt.id,
                payload={"answer": answer, "error": error_text},
            )

        stored_run = self.store.get_run(run.id) or run
        trace = RunTrace(
            run_id=run.id,
            workdir=run.workdir,
            task=task,
            model=model or run.model,
            status=stored_run.status.value,
            attempt_id=attempt.id,
            steps=context.trace_steps,
            events=self.store.events.read(run.id),
        )
        trace_path = trace.dump(self.trace_root / run.id / "trace.json")
        return RuntimeResult(stored_run, attempt, answer, trace, trace_path)

    def run_agent(
        self,
        cfg,
        task: str,
        workdir: str,
        *,
        agent: Any = None,
        reset: bool = True,
        run_id: str | None = None,
        project_id: str = "default",
        user_id: str = "default",
        max_duration: float | None = None,
    ) -> RuntimeResult:
        """Run a smolagents instance and persist only this invocation's steps."""
        before = len(getattr(getattr(agent, "memory", None), "steps", []) or []) if agent else 0
        built_agent = agent

        def execute(context: RuntimeContext) -> str:
            nonlocal built_agent
            if built_agent is None:
                from ..harness import make_agent

                built_agent = make_agent(
                    cfg,
                    workdir,
                    event_sink=lambda event: self._handle_tool_event(context, event),
                    project_id=project_id,
                    user_id=user_id,
                    run_id=context.run.id,
                    cancel_event=context.cancel_event,
                )
                with self._lock:
                    self._active[context.run.id] = (built_agent, context.cancel_event, context)
            self._bind_agent_events(built_agent, context)
            try:
                kwargs: dict[str, Any] = {"reset": reset}
                try:
                    signature = inspect.signature(built_agent.run)
                    if "max_steps" in signature.parameters or any(
                        parameter.kind == inspect.Parameter.VAR_KEYWORD
                        for parameter in signature.parameters.values()
                    ):
                        kwargs["max_steps"] = context.run.max_steps
                except (TypeError, ValueError):
                    pass
                output = built_agent.run(task, **kwargs)
            finally:
                new_steps = self._agent_steps(built_agent, before)
                context.trace_steps.extend(new_steps)
                self._persist_agent_steps(context, new_steps)
            return output

        result = self.run(
            task,
            workdir,
            execute,
            run_id=run_id,
            model=getattr(cfg, "model", ""),
            project_id=project_id,
            user_id=user_id,
            max_steps=int(getattr(cfg, "max_steps", 20)),
            max_duration=max_duration,
            resume=not reset,
            agent=built_agent,
        )
        result.agent = built_agent
        return result

    def cancel(self, run_id: str) -> Run | None:
        run = self.store.get_run(run_id)
        if run is None:
            return None
        if run.status in {RunStatus.SUCCEEDED, RunStatus.FAILED, RunStatus.CANCELLED}:
            return run
        with self._lock:
            active = self._active.get(run_id)
            if active:
                agent, event, _ = active
                event.set()
                if agent is not None and hasattr(agent, "interrupt_switch"):
                    agent.interrupt_switch = True
        run = self.store.update_run(run_id, cancel_requested=True, status=RunStatus.CANCELLED)
        if run:
            self.emit(run_id, "run.cancel_requested", payload={})
        return run

    def resume(
        self,
        run_id: str,
        executor: Callable[[RuntimeContext], Any],
        *,
        agent: Any = None,
    ) -> RuntimeResult:
        run = self.store.get_run(run_id)
        if run is None:
            raise ValueError(f"unknown run: {run_id}")
        checkpoint = self.store.latest_checkpoint(run_id)
        return self.run(
            run.task,
            run.workdir,
            executor,
            run_id=run_id,
            model=run.model,
            project_id=run.project_id,
            user_id=run.user_id,
            max_steps=run.max_steps,
            max_duration=run.max_duration,
            resume=True,
            agent=agent,
            trace_steps=(checkpoint.state.get("trace_steps", []) if checkpoint else []),
        )

    def _timeout(self, run_id: str) -> None:
        with self._lock:
            active = self._active.get(run_id)
            if not active:
                return
            agent, event, context = active
            context.timed_out = True
            event.set()
            if agent is not None and hasattr(agent, "interrupt_switch"):
                agent.interrupt_switch = True

    def _handle_tool_event(self, context: RuntimeContext, event: dict[str, Any]) -> None:
        phase = event.get("phase")
        call_id = str(event.get("call_id", ""))
        if not call_id:
            return
        if phase == "started":
            call = ToolCall.create(
                context.run.id,
                context.attempt.id,
                str(event.get("name", "unknown")),
                arguments=event.get("arguments") or {},
                idempotency_key=f"{context.attempt.id}:{call_id}",
            )
            context.active_tool_calls[call_id] = call
            self.store.append_tool_call(call)
            self.emit(
                context.run.id,
                "tool_call.started",
                attempt_id=context.attempt.id,
                payload=call.to_record(),
            )
            return
        if phase != "completed":
            if phase == "policy":
                self.emit(
                    context.run.id,
                    "policy.decision",
                    attempt_id=context.attempt.id,
                    payload={key: value for key, value in event.items() if key != "phase"},
                )
            return
        completed_call = context.active_tool_calls.get(call_id)
        if completed_call is None:
            return
        del context.active_tool_calls[call_id]
        error = event.get("error")
        completed_call.status = StepStatus.FAILED if error else StepStatus.SUCCEEDED
        completed_call.finished_at = time.time()
        completed_call.observation = event.get("observation")
        completed_call.error = str(error) if error else None
        self.store.update_tool_call(
            completed_call.id,
            status=completed_call.status,
            finished_at=completed_call.finished_at,
            observation=completed_call.observation,
            error=completed_call.error,
        )
        self.emit(
            context.run.id,
            "tool_call.completed",
            attempt_id=context.attempt.id,
            payload=completed_call.to_record(),
        )

    def _bind_agent_events(self, agent: Any, context: RuntimeContext) -> None:
        """Rebind instrumented tools when one agent handles multiple runs."""
        tools = getattr(agent, "tools", {}) if agent is not None else {}
        values = tools.values() if hasattr(tools, "values") else tools
        for tool in values or []:
            if hasattr(tool, "event_sink"):
                tool.event_sink = lambda event, current=context: self._handle_tool_event(current, event)
            inner = getattr(tool, "inner", None)
            sandbox = getattr(inner, "sandbox", None) or getattr(tool, "sandbox", None)
            if sandbox is not None and hasattr(sandbox, "cancel_event"):
                sandbox.cancel_event = context.cancel_event

    def _persist_agent_steps(self, context: RuntimeContext, raw_steps: list[dict[str, Any]]) -> None:
        for index, raw in enumerate(raw_steps, start=1):
            kind = str(raw.get("kind", "step"))
            number = int(raw.get("step", index) or index)
            observation = str(raw.get("observation", ""))
            output = raw.get("output", observation)
            status = StepStatus.FAILED if observation.startswith("Error:") else StepStatus.SUCCEEDED
            step = Step.create(
                context.run.id,
                context.attempt.id,
                number,
                kind,
                input=raw.get("tool_calls") or raw.get("task") or raw.get("plan"),
                output=output,
                status=status,
                finished_at=time.time(),
                error=observation if status == StepStatus.FAILED else None,
                metadata={"source": "smolagents", "raw": raw},
            )
            try:
                self.store.append_step(step)
            except Exception:
                # A malformed third-party memory item should not hide the
                # agent result; its event remains available in the trace.
                continue
            self.emit(
                context.run.id,
                "step.completed",
                attempt_id=context.attempt.id,
                payload=step.to_record(),
            )

    @staticmethod
    def _agent_steps(agent: Any, start: int) -> list[dict[str, Any]]:
        if agent is None:
            return []
        trace = RunTrace.from_smol_agent(
            agent,
            run_id="snapshot",
            workdir="",
            task="",
            start_index=start,
        )
        return trace.steps

    def _result_from_stored(self, run: Run) -> RuntimeResult:
        attempts = self.store.list_attempts(run.id)
        attempt = attempts[-1] if attempts else Attempt.create(run.id, 0)
        events = self.store.events.read(run.id)
        trace_path = self.trace_root / run.id / "trace.json"
        if trace_path.exists():
            trace = RunTrace.load(trace_path)
        else:
            trace = RunTrace(
                run_id=run.id,
                workdir=run.workdir,
                task=run.task,
                model=run.model,
                status=run.status.value,
                attempt_id=attempt.id,
                events=events,
            )
            trace_path = trace.dump(trace_path)
        return RuntimeResult(run, attempt, run.answer or "", trace, trace_path)
