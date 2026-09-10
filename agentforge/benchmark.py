"""Reproducible benchmark runner and report generation."""

from __future__ import annotations

import json
import os
import platform
import statistics
import subprocess
import sys
import tempfile
import time
from collections import Counter
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any

from . import __version__
from .config import ModelConfig
from .eval import CodeTask, evaluate


@dataclass(frozen=True)
class BenchmarkOptions:
    """Explicit controls for one reproducible benchmark variant."""

    context_compression_enabled: bool = True
    verify_enabled: bool = False
    max_attempts: int = 1

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            raise ValueError("verify_attempts must be >= 1")
        if not self.verify_enabled and self.max_attempts != 1:
            raise ValueError("verify_attempts requires verify_retry")
        if self.verify_enabled and self.max_attempts < 2:
            raise ValueError("verify_retry requires verify_attempts >= 2")

    @property
    def experiment_id(self) -> str:
        if not self.verify_enabled and not self.context_compression_enabled and self.max_attempts == 1:
            return "A"
        if not self.verify_enabled and self.context_compression_enabled and self.max_attempts == 1:
            return "B"
        if self.verify_enabled and not self.context_compression_enabled and self.max_attempts == 3:
            return "C"
        if self.verify_enabled and self.context_compression_enabled and self.max_attempts == 3:
            return "D"
        return "custom"

    def to_dict(self) -> dict[str, Any]:
        return {
            "experiment_id": self.experiment_id,
            "context_compression_enabled": self.context_compression_enabled,
            "verify_enabled": self.verify_enabled,
            "max_attempts": self.max_attempts,
        }


def git_metadata() -> dict[str, Any]:
    """Return commit and dirty state without ever reading secret files."""
    root = Path(__file__).resolve().parents[1]
    try:
        commit = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=root,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        dirty = bool(
            subprocess.run(
                ["git", "status", "--porcelain"],
                cwd=root,
                capture_output=True,
                text=True,
                check=True,
            ).stdout.strip()
        )
        return {"commit": commit, "dirty": dirty}
    except (OSError, subprocess.CalledProcessError):
        return {"commit": None, "dirty": None}


def public_config(cfg: ModelConfig) -> dict[str, Any]:
    record = asdict(cfg)
    if record.get("api_key"):
        record["api_key"] = "<redacted>"
    return record


def run_benchmark(
    cfg: ModelConfig,
    tasks: list[CodeTask],
    *,
    num_trials: int = 1,
    k: int = 1,
    out_dir: str | Path = "runs/benchmarks",
    seed: int = 0,
    options: BenchmarkOptions | None = None,
) -> dict[str, Any]:
    """Run a fixed task list and atomically write a complete report."""
    options = options or BenchmarkOptions(
        context_compression_enabled=cfg.context_compression_enabled,
    )
    effective_cfg = replace(
        cfg,
        context_compression_enabled=options.context_compression_enabled,
    )
    started = time.time()
    result = evaluate(
        effective_cfg,
        tasks,
        num_trials=num_trials,
        k=k,
        out_dir=out_dir,
        verify_enabled=options.verify_enabled,
        max_attempts=options.max_attempts,
    )
    episodes = result["episodes"]
    first_success = sum(int(item.get("first_reward", item["reward"])) for item in episodes)
    final_success = sum(int(item["reward"]) for item in episodes)
    latencies = [float(item.get("elapsed_seconds", 0.0)) for item in episodes]
    steps = [int(item.get("steps", 0)) for item in episodes]
    input_tokens = [int(item.get("input_tokens", 0)) for item in episodes]
    output_tokens = [int(item.get("output_tokens", 0)) for item in episodes]
    first_input_tokens = [int(item.get("first_input_tokens", 0)) for item in episodes]
    first_output_tokens = [int(item.get("first_output_tokens", 0)) for item in episodes]
    attempt_counts = [int(item.get("attempt_count", 1)) for item in episodes]
    compression_triggered = [
        item for item in episodes if int(item.get("compression_trigger_count", 0)) > 0
    ]
    compression_requests = sum(len(item.get("compression_stats", [])) for item in episodes)
    attempt_failure_types = Counter(
        str(attempt["failure_type"])
        for item in episodes
        for attempt in item.get("attempts", [])
        if attempt.get("failure_type")
    )
    failures = Counter(
        str(item["failure_type"])
        for item in episodes
        if item.get("failure_type")
    )
    input_rate = float(getattr(cfg, "input_cost_per_million", 0.0))
    output_rate = float(getattr(cfg, "output_cost_per_million", 0.0))
    estimated_cost = sum(input_tokens) / 1_000_000 * input_rate + sum(output_tokens) / 1_000_000 * output_rate
    first_cost = sum(first_input_tokens) / 1_000_000 * input_rate + sum(first_output_tokens) / 1_000_000 * output_rate
    git = git_metadata()
    episode_count = len(episodes)
    report: dict[str, Any] = {
        "schema_version": 2,
        "benchmark_version": "v2",
        "agentforge_version": __version__,
        "generated_at": time.time(),
        "duration_seconds": time.time() - started,
        "python": sys.version,
        "platform": platform.platform(),
        "model": effective_cfg.model,
        "config": public_config(effective_cfg),
        "experiment_id": options.experiment_id,
        "experiment": options.to_dict(),
        "git": git,
        "git_commit": git["commit"],
        "workspace_dirty": git["dirty"],
        "task_order": [task.name for task in tasks],
        "seed": seed,
        "task_specs": [task.to_spec() for task in tasks],
        "task_summary": {
            "count": len(tasks),
            "names": [task.name for task in tasks],
            "versions": {task.name: task.task_version for task in tasks},
        },
        "summary": {
            "tasks": len(tasks),
            "episodes": episode_count,
            "num_trials": num_trials,
            "k": k,
            "pass@1": result["pass@1"],
            "pass@k": result["pass@k"],
            "pass@3": result.get("pass@3"),
            "pass@5": result.get("pass@5"),
            "first_success_rate": first_success / episode_count if episode_count else 0.0,
            "final_success_rate": final_success / episode_count if episode_count else 0.0,
            "verify_gain": (
                (final_success - first_success) / episode_count if episode_count else 0.0
            ),
            "avg_attempts": statistics.fmean(attempt_counts) if attempt_counts else 0.0,
            "avg_steps": statistics.fmean(steps) if steps else 0.0,
            "avg_input_tokens": statistics.fmean(input_tokens) if input_tokens else 0.0,
            "avg_output_tokens": statistics.fmean(output_tokens) if output_tokens else 0.0,
            "estimated_cost": estimated_cost,
            "first_attempt_cost": first_cost,
            "verify_incremental_input_tokens": sum(input_tokens) - sum(first_input_tokens),
            "verify_incremental_output_tokens": sum(output_tokens) - sum(first_output_tokens),
            "verify_incremental_cost": estimated_cost - first_cost,
            "compression_trigger_rate": len(compression_triggered) / episode_count if episode_count else 0.0,
            "compression_triggered_episodes": len(compression_triggered),
            "compression_requests": compression_requests,
            "compression_dropped_steps": sum(
                int(item.get("compression_dropped_steps", 0)) for item in episodes
            ),
            "compression_saved_chars": sum(
                int(item.get("compression_saved_chars", 0)) for item in episodes
            ),
            "compression_saved_tokens": sum(
                int(item.get("compression_saved_tokens", 0)) for item in episodes
            ),
            "p50_latency_seconds": _percentile(latencies, 0.50),
            "p95_latency_seconds": _percentile(latencies, 0.95),
            "failure_types": dict(failures),
            "attempt_failure_types": dict(attempt_failure_types),
        },
        "failures": result["failures"],
        "episodes": episodes,
    }
    target = Path(out_dir)
    target.mkdir(parents=True, exist_ok=True)
    report_path = target / "report.json"
    report["report_path"] = str(report_path)
    _atomic_json(report_path, report)
    _atomic_json(target / "episodes.json", episodes)
    return report


def load_report(path: str | Path) -> dict[str, Any]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("benchmark report must be a JSON object")
    try:
        schema_version = int(data.get("schema_version", 1))
    except (TypeError, ValueError) as exc:
        raise ValueError("benchmark report schema_version must be an integer") from exc
    if schema_version not in {1, 2}:
        raise ValueError(f"unsupported benchmark report schema_version: {schema_version}")
    data["schema_version"] = schema_version
    return data


def _percentile(values: list[float], fraction: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(round((len(ordered) - 1) * fraction))))
    return ordered[index]


def _atomic_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(data, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            try:
                os.unlink(temporary)
            except OSError:
                pass
