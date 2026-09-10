"""FastAPI endpoints backed by the same runtime used by the CLI."""

from __future__ import annotations

import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from .. import __version__
from ..benchmark import public_config
from ..benchmark_store import BenchmarkStore
from ..config import ModelConfig, load_config
from ..observability import MetricsRegistry, configure_tracing
from ..runtime import AgentRuntime, RunStatus, RuntimeStore

try:
    from fastapi import FastAPI, HTTPException
    from fastapi.responses import HTMLResponse
    from pydantic import BaseModel, Field
except ImportError as exc:  # pragma: no cover - exercised when optional extra is absent
    FastAPI = None  # type: ignore
    HTMLResponse = None  # type: ignore
    _FASTAPI_ERROR = exc

    class BaseModel:  # type: ignore[no-redef]
        pass

    def Field(default=None, **kwargs):  # type: ignore[no-redef]
        return default


class RunCreateRequest(BaseModel):
    repo: str
    task: str = Field(min_length=1)
    run_id: str | None = None
    project_id: str = "default"
    user_id: str = "default"
    max_duration: float | None = Field(default=None, gt=0)


class BenchmarkRequest(BaseModel):
    trials: int = Field(default=1, ge=1)
    k: int = Field(default=1, ge=1)
    seed: int = 0
    tasks: list[str] | None = None


class RunService:
    def __init__(self, cfg: ModelConfig):
        self.cfg = cfg
        configure_tracing()
        self.runtime = AgentRuntime(
            RuntimeStore(cfg.state_db, cfg.trace_dir),
            trace_root=cfg.trace_dir,
        )
        self.runtime.store.recover_stale_runs()
        self.benchmark_store = BenchmarkStore(cfg.state_db)
        self.benchmark_store.recover_running()
        self.pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="agentforge")
        self.metrics = MetricsRegistry()
        self._lock = threading.RLock()
        self._resume_pending: set[str] = set()
        self._closed = False

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
        self.pool.shutdown(wait=True, cancel_futures=True)
        self.runtime.store.close()
        self.benchmark_store.close()

    def submit(self, request: RunCreateRequest) -> dict[str, Any]:
        workdir = _validate_workdir(request.repo)
        run = self.runtime.create_run(
            request.task,
            workdir,
            run_id=request.run_id,
            model=self.cfg.model,
            project_id=request.project_id,
            user_id=request.user_id,
            max_steps=self.cfg.max_steps,
            max_duration=request.max_duration,
        )
        self.pool.submit(self._run, run.id, request, workdir)
        return _run_json(run)

    def _run(self, run_id: str, request: RunCreateRequest, workdir: str) -> None:
        started = __import__("time").perf_counter()
        try:
            result = self.runtime.run_agent(
                self.cfg,
                request.task,
                workdir,
                run_id=run_id,
                project_id=request.project_id,
                user_id=request.user_id,
                max_duration=request.max_duration,
            )
            self._record_run_metrics(result.run.id, result.run.status.value, result.attempt.id)
        except Exception:
            self.metrics.inc("agentforge_runs_total", status="failed")
        finally:
            self.metrics.observe(
                "agentforge_run_duration_seconds",
                __import__("time").perf_counter() - started,
            )

    def _record_run_metrics(self, run_id: str, status: str, attempt_id: str | None = None) -> None:
        self.metrics.inc("agentforge_runs_total", status=status)
        for event in self.runtime.store.events.read(run_id):
            if attempt_id is not None and event.get("attempt_id") != attempt_id:
                continue
            event_type = str(event.get("type", ""))
            payload = event.get("payload") or {}
            if event_type == "tool_call.completed":
                self.metrics.inc(
                    "agentforge_tool_calls_total",
                    status="failed" if payload.get("error") else "succeeded",
                )
            elif event_type == "llm.retry":
                self.metrics.inc("agentforge_llm_retries_total")
            elif event_type == "policy.decision" and payload.get("allowed") is False:
                self.metrics.inc("agentforge_sandbox_rejections_total")
            elif event_type == "verification.completed":
                self.metrics.inc(
                    "agentforge_verifications_total",
                    status="passed" if payload.get("reward") else "failed",
                )

    def benchmark(self, request: BenchmarkRequest) -> dict[str, Any]:
        from ..benchmark import run_benchmark
        from ..code_tasks import select_benchmark_tasks

        if request.k > request.trials:
            raise ValueError("k must be <= trials")
        tasks = select_benchmark_tasks(request.tasks)
        benchmark_id = f"benchmark_{uuid.uuid4().hex}"
        record = {
            "id": benchmark_id,
            "status": "running",
            "trials": request.trials,
            "k": request.k,
            "seed": request.seed,
            "tasks": [task.name for task in tasks],
            "config": public_config(self.cfg),
        }
        self.benchmark_store.create(record)

        def worker() -> None:
            try:
                result = run_benchmark(
                    self.cfg,
                    tasks,
                    num_trials=request.trials,
                    k=request.k,
                    seed=request.seed,
                    out_dir=Path(self.cfg.trace_dir) / "benchmarks" / benchmark_id,
                )
                self.benchmark_store.update(
                    benchmark_id,
                    status="succeeded",
                    report_path=result.get("report_path"),
                )
            except Exception as exc:
                self.benchmark_store.update(
                    benchmark_id,
                    status="failed",
                    error=f"{type(exc).__name__}: {exc}",
                )

        self.pool.submit(worker)
        return self.benchmark_store.get(benchmark_id) or record

    def get_benchmark(self, benchmark_id: str) -> dict[str, Any] | None:
        return self.benchmark_store.get(benchmark_id)

    def resume(self, run_id: str) -> dict[str, Any]:
        with self._lock:
            if run_id in self._resume_pending:
                raise ValueError(f"run {run_id} already has a resume in progress")
            run = self.runtime.store.get_run(run_id)
            if run is None:
                raise KeyError("run not found")
            if run.status not in {RunStatus.FAILED, RunStatus.CANCELLED}:
                raise ValueError(f"run {run_id} is not resumable from status {run.status.value}")
            self._resume_pending.add(run_id)
        existing = self.runtime.store.list_attempts(run_id)
        holder: dict[str, Any] = {}
        ready = threading.Event()

        def on_attempt(attempt) -> None:
            holder["attempt_id"] = attempt.id
            ready.set()

        def worker() -> None:
            started = __import__("time").perf_counter()
            try:
                result = self.runtime.run_agent(
                    self.cfg,
                    run.task,
                    run.workdir,
                    run_id=run.id,
                    project_id=run.project_id,
                    user_id=run.user_id,
                    reset=False,
                    max_duration=run.max_duration,
                    attempt_callback=on_attempt,
                )
                self._record_run_metrics(result.run.id, result.run.status.value, result.attempt.id)
            except Exception:
                self.metrics.inc("agentforge_runs_total", status="failed")
            finally:
                self.metrics.observe(
                    "agentforge_run_duration_seconds",
                    __import__("time").perf_counter() - started,
                )
                with self._lock:
                    self._resume_pending.discard(run_id)

        try:
            self.pool.submit(worker)
        except Exception:
            with self._lock:
                self._resume_pending.discard(run_id)
            raise
        ready.wait(timeout=2.0)
        current = self.runtime.store.get_run(run_id) or run
        result = _run_json(current)
        result["resumed"] = True
        result["previous_attempt_count"] = len(existing)
        result["attempt_id"] = holder.get("attempt_id")
        return result


def create_app(cfg: ModelConfig | None = None, service: RunService | None = None):
    if FastAPI is None:
        raise RuntimeError("API requires fastapi and uvicorn; install agentforge[api]") from _FASTAPI_ERROR
    cfg = cfg or load_config()
    service = service or RunService(cfg)
    @asynccontextmanager
    async def lifespan(_app):
        try:
            yield
        finally:
            service.close()

    app = FastAPI(title="AgentForge", version=__version__, lifespan=lifespan)
    app.state.agentforge = service

    @app.get("/", response_class=HTMLResponse)
    def index():
        page = Path(__file__).with_name("static") / "index.html"
        return page.read_text(encoding="utf-8")

    @app.post("/runs", status_code=202)
    def create_run(request: RunCreateRequest):
        try:
            return service.submit(request)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/runs/{run_id}")
    def get_run(run_id: str):
        run = service.runtime.store.get_run(run_id)
        if run is None:
            raise HTTPException(status_code=404, detail="run not found")
        return _run_json(run)

    @app.get("/runs/{run_id}/trace")
    def get_trace(run_id: str):
        run = service.runtime.store.get_run(run_id)
        if run is None:
            raise HTTPException(status_code=404, detail="run not found")
        return {
            "run": _run_json(run),
            "attempts": [item.to_record() for item in service.runtime.store.list_attempts(run_id)],
            "steps": [item.to_record() for item in service.runtime.store.list_steps(run_id)],
            "tool_calls": [item.to_record() for item in service.runtime.store.list_tool_calls(run_id)],
            "verifications": [
                item.to_record() for item in service.runtime.store.list_verifications(run_id)
            ],
            "events": service.runtime.store.events.read(run_id),
        }

    @app.post("/runs/{run_id}/cancel")
    def cancel_run(run_id: str):
        run = service.runtime.cancel(run_id)
        if run is None:
            raise HTTPException(status_code=404, detail="run not found")
        return _run_json(run)

    @app.post("/benchmarks", status_code=202)
    def create_benchmark(request: BenchmarkRequest):
        try:
            return service.benchmark(request)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/benchmarks/{benchmark_id}")
    def get_benchmark(benchmark_id: str):
        record = service.get_benchmark(benchmark_id)
        if record is None:
            raise HTTPException(status_code=404, detail="benchmark not found")
        return record

    @app.post("/runs/{run_id}/resume", status_code=202)
    def resume_run(run_id: str):
        try:
            return service.resume(run_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.get("/metrics")
    def metrics():
        from fastapi.responses import PlainTextResponse

        return PlainTextResponse(service.metrics.render(), media_type="text/plain; version=0.0.4")

    return app


def _validate_workdir(value: str) -> str:
    path = Path(value).expanduser().resolve()
    if not path.is_dir():
        raise ValueError(f"repo is not a directory: {value}")
    return str(path)


def _run_json(run) -> dict[str, Any]:
    record = run.to_record()
    record["status"] = str(record["status"])
    record["trace_id"] = record.get("metadata", {}).get("trace_id")
    return record
