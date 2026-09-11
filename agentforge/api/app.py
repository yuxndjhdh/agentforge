"""FastAPI endpoints backed by the same runtime used by the CLI."""

from __future__ import annotations

import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from dataclasses import replace
from pathlib import Path
from typing import Any

from .. import __version__
from ..benchmark import BenchmarkOptions, public_config
from ..benchmark_store import BenchmarkStore
from ..config import ModelConfig, load_config
from ..observability import MetricsRegistry, configure_tracing
from ..runtime import AgentRuntime, RunStatus, RuntimeStore
from .details import (
    checkpoint_status,
    run_config,
    serialize_checkpoints,
    summarize_run,
    workspace_diff,
)

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
    model: str | None = Field(default=None, min_length=1)
    sandbox_backend: str | None = None
    max_steps: int | None = Field(default=None, ge=1, le=1000)
    verify_command: str | None = Field(default=None, min_length=1)
    verify_attempts: int = Field(default=1, ge=1, le=10)


class BenchmarkRequest(BaseModel):
    trials: int = Field(default=1, ge=1)
    k: int = Field(default=1, ge=1)
    seed: int = 0
    tasks: list[str] | None = None
    context_compression: bool | None = None
    verify_retry: bool = False
    verify_attempts: int | None = Field(default=None, ge=1)


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
        effective_cfg = self._config_for_request(request)
        verify_command = str(request.verify_command or "").strip() or None
        if request.verify_attempts != 1 and verify_command is None:
            raise ValueError("verify_attempts requires verify_command")
        run = self.runtime.create_run(
            request.task,
            workdir,
            run_id=request.run_id,
            model=effective_cfg.model,
            project_id=request.project_id,
            user_id=request.user_id,
            max_steps=effective_cfg.max_steps,
            max_duration=request.max_duration,
            metadata={
                "config": public_config(effective_cfg),
                "verification": {
                    "enabled": verify_command is not None,
                    "command": verify_command,
                    "attempts": request.verify_attempts,
                },
                "sandbox": {
                    "backend": effective_cfg.sandbox_backend,
                    "fallback_warning": effective_cfg.sandbox_backend == "auto",
                },
            },
        )
        self.pool.submit(self._run, run.id, request, workdir)
        return _run_json(run)

    def _config_for_request(self, request: RunCreateRequest) -> ModelConfig:
        backend = request.sandbox_backend
        if backend is not None:
            backend = backend.strip().lower()
            if backend not in {"auto", "local", "docker", "podman"}:
                raise ValueError("sandbox_backend must be auto, local, docker, or podman")
        overrides: dict[str, Any] = {}
        if request.model is not None:
            overrides["model"] = request.model.strip()
        if backend is not None:
            overrides["sandbox_backend"] = backend
        if request.max_steps is not None:
            overrides["max_steps"] = request.max_steps
        return replace(self.cfg, **overrides)

    def _config_for_run(self, run) -> ModelConfig:
        configured = (run.metadata or {}).get("config")
        if not isinstance(configured, dict):
            return self.cfg
        allowed = {
            "base_url", "model", "temperature", "max_steps", "max_context_chars",
            "max_context_tokens", "tokenizer", "context_min_tail", "sandbox_whitelist",
            "sandbox_denylist", "sandbox_deny_patterns", "sandbox_readonly", "sandbox_timeout",
            "sandbox_max_output_bytes", "sandbox_backend", "sandbox_image", "sandbox_network",
            "sandbox_cpu_limit", "sandbox_memory_limit_mb", "sandbox_pids_limit",
            "sandbox_disk_limit_mb", "llm_timeout", "llm_retries", "llm_backoff",
            "input_cost_per_million", "output_cost_per_million", "context_compression_enabled",
        }
        overrides = {key: configured[key] for key in allowed if key in configured}
        try:
            return replace(self.cfg, **overrides)
        except (TypeError, ValueError):
            return self.cfg

    @staticmethod
    def _verification_for_run(run) -> tuple[str | None, int]:
        record = (run.metadata or {}).get("verification")
        if not isinstance(record, dict):
            return None, 1
        command = str(record.get("command") or "").strip() or None
        try:
            attempts = max(1, int(record.get("attempts", 1)))
        except (TypeError, ValueError):
            attempts = 1
        return command, attempts

    def _run(self, run_id: str, request: RunCreateRequest, workdir: str) -> None:
        started = __import__("time").perf_counter()
        try:
            effective_cfg = self._config_for_request(request)
            result = self.runtime.run_agent(
                effective_cfg,
                request.task,
                workdir,
                run_id=run_id,
                project_id=request.project_id,
                user_id=request.user_id,
                max_duration=request.max_duration,
                verify_command=str(request.verify_command or "").strip() or None,
                verify_attempts=request.verify_attempts,
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
        explicit_options = (
            request.context_compression is not None
            or request.verify_retry
            or request.verify_attempts is not None
        )
        options = BenchmarkOptions(
            context_compression_enabled=(
                self.cfg.context_compression_enabled
                if request.context_compression is None
                else request.context_compression
            ),
            verify_enabled=request.verify_retry,
            max_attempts=(
                request.verify_attempts
                if request.verify_attempts is not None
                else (3 if request.verify_retry else 1)
            ),
        )
        benchmark_id = f"benchmark_{uuid.uuid4().hex}"
        record = {
            "id": benchmark_id,
            "status": "running",
            "trials": request.trials,
            "k": request.k,
            "seed": request.seed,
            "tasks": [task.name for task in tasks],
            "config": public_config(self.cfg),
            "experiment_id": options.experiment_id,
            "experiment": options.to_dict(),
        }
        self.benchmark_store.create(record)

        def worker() -> None:
            try:
                benchmark_kwargs: dict[str, Any] = {
                    "num_trials": request.trials,
                    "k": request.k,
                    "seed": request.seed,
                    "out_dir": Path(self.cfg.trace_dir) / "benchmarks" / benchmark_id,
                }
                if explicit_options:
                    benchmark_kwargs["options"] = options
                result = run_benchmark(self.cfg, tasks, **benchmark_kwargs)
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

    def get_run_detail(self, run_id: str) -> dict[str, Any] | None:
        run = self.runtime.store.get_run(run_id)
        if run is None:
            return None
        attempts = self.runtime.store.list_attempts(run_id)
        steps = self.runtime.store.list_steps(run_id)
        tool_calls = self.runtime.store.list_tool_calls(run_id)
        verifications = self.runtime.store.list_verifications(run_id)
        checkpoints = self.runtime.store.list_checkpoints(run_id)
        events = self.runtime.store.events.read(run_id)
        trace_record: dict[str, Any] = {}
        trace_path = Path(self.cfg.trace_dir) / run_id / "trace.json"
        if trace_path.is_file():
            try:
                import json

                loaded = json.loads(trace_path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    trace_record = loaded
            except (OSError, ValueError):
                trace_record = {}
        config = run_config(run, public_config(self.cfg))
        return {
            "run": _run_json(run),
            "attempts": [item.to_record() for item in attempts],
            "steps": [item.to_record() for item in steps],
            "tool_calls": [item.to_record() for item in tool_calls],
            "verifications": [item.to_record() for item in verifications],
            "checkpoints": serialize_checkpoints(checkpoints),
            "checkpoint": checkpoint_status(checkpoints),
            "events": events,
            "metrics": summarize_run(
                run,
                attempts,
                steps,
                tool_calls,
                verifications,
                checkpoints,
                events,
                config=config,
                trace=trace_record,
            ),
            "config": {
                "model": config.get("model"),
                "sandbox_backend": config.get("sandbox_backend"),
                "sandbox_network": config.get("sandbox_network"),
                "sandbox_readonly": config.get("sandbox_readonly"),
                "sandbox_timeout": config.get("sandbox_timeout"),
                "sandbox_cpu_limit": config.get("sandbox_cpu_limit"),
                "sandbox_memory_limit_mb": config.get("sandbox_memory_limit_mb"),
                "sandbox_pids_limit": config.get("sandbox_pids_limit"),
                "sandbox_disk_limit_mb": config.get("sandbox_disk_limit_mb"),
                "fallback_warning": config.get("sandbox_backend") == "auto",
            },
            "verification_policy": (run.metadata or {}).get("verification", {"enabled": False}),
            "diff": workspace_diff(run.workdir),
        }

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
        checkpoint = self.runtime.store.latest_checkpoint(run_id)
        checkpoint_sequence = checkpoint.sequence if checkpoint is not None else None
        holder: dict[str, Any] = {}
        ready = threading.Event()

        def on_attempt(attempt) -> None:
            holder["attempt_id"] = attempt.id
            ready.set()

        def worker() -> None:
            started = __import__("time").perf_counter()
            try:
                verify_command, verify_attempts = self._verification_for_run(run)
                result = self.runtime.run_agent(
                    self._config_for_run(run),
                    run.task,
                    run.workdir,
                    run_id=run.id,
                    project_id=run.project_id,
                    user_id=run.user_id,
                    reset=False,
                    max_duration=run.max_duration,
                    attempt_callback=on_attempt,
                    verify_command=verify_command,
                    verify_attempts=verify_attempts,
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
        result["checkpoint_sequence"] = checkpoint_sequence
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

    @app.get("/config")
    def get_config():
        return {
            "version": __version__,
            "model": service.cfg.model,
            "max_steps": service.cfg.max_steps,
            "sandbox": {
                "configured_backend": service.cfg.sandbox_backend,
                "allowed_backends": ["local", "docker", "podman", "auto"],
                "fallback_warning": service.cfg.sandbox_backend == "auto",
                "network": service.cfg.sandbox_network,
                "readonly": service.cfg.sandbox_readonly,
                "timeout": service.cfg.sandbox_timeout,
                "cpu_limit": service.cfg.sandbox_cpu_limit,
                "memory_limit_mb": service.cfg.sandbox_memory_limit_mb,
                "pids_limit": service.cfg.sandbox_pids_limit,
                "disk_limit_mb": service.cfg.sandbox_disk_limit_mb,
            },
        }

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
        detail = service.get_run_detail(run_id)
        if detail is None:
            raise HTTPException(status_code=404, detail="run not found")
        return detail

    @app.get("/runs/{run_id}/summary")
    def get_run_summary(run_id: str):
        detail = service.get_run_detail(run_id)
        if detail is None:
            raise HTTPException(status_code=404, detail="run not found")
        return {
            "run": detail["run"],
            "metrics": detail["metrics"],
            "checkpoint": detail["checkpoint"],
            "config": detail["config"],
            "verification_policy": detail["verification_policy"],
            "diff": detail["diff"],
        }

    @app.get("/runs/{run_id}/export")
    def export_run(run_id: str):
        from fastapi.responses import JSONResponse

        detail = service.get_run_detail(run_id)
        if detail is None:
            raise HTTPException(status_code=404, detail="run not found")
        return JSONResponse(
            content=detail,
            headers={"Content-Disposition": f'attachment; filename="agentforge-{run_id}.json"'},
        )

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
