"""FastAPI endpoints backed by the same runtime used by the CLI."""

from __future__ import annotations

import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from ..config import ModelConfig, load_config
from ..observability import MetricsRegistry
from ..runtime import AgentRuntime, RuntimeStore

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
        self.runtime = AgentRuntime(
            RuntimeStore(cfg.state_db, cfg.trace_dir),
            trace_root=cfg.trace_dir,
        )
        self.runtime.store.recover_stale_runs()
        self.pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="agentforge")
        self.benchmarks: dict[str, dict[str, Any]] = {}
        self.metrics = MetricsRegistry()
        self._lock = threading.RLock()

    def close(self) -> None:
        self.pool.shutdown(wait=False, cancel_futures=True)
        self.runtime.store.close()

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
            self.metrics.inc("agentforge_runs_total", status=result.run.status.value)
        except Exception:
            self.metrics.inc("agentforge_runs_total", status="failed")
        finally:
            self.metrics.observe(
                "agentforge_run_duration_seconds",
                __import__("time").perf_counter() - started,
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
        }
        with self._lock:
            self.benchmarks[benchmark_id] = record

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
                with self._lock:
                    record.update(result, status="succeeded")
            except Exception as exc:
                with self._lock:
                    record.update(status="failed", error=f"{type(exc).__name__}: {exc}")

        self.pool.submit(worker)
        return record.copy()


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

    app = FastAPI(title="AgentForge", version="0.2.0", lifespan=lifespan)
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
        with service._lock:
            record = service.benchmarks.get(benchmark_id)
        if record is None:
            raise HTTPException(status_code=404, detail="benchmark not found")
        return record

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
    return record
