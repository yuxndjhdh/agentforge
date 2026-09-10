"""Reproducible benchmark runner and report generation."""

from __future__ import annotations

import json
import os
import platform
import statistics
import sys
import tempfile
import time
from collections import Counter
from dataclasses import asdict
from pathlib import Path
from typing import Any

from . import __version__
from .config import ModelConfig
from .eval import CodeTask, evaluate


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
) -> dict[str, Any]:
    """Run a fixed task list and atomically write a complete report."""
    started = time.time()
    result = evaluate(cfg, tasks, num_trials=num_trials, k=k, out_dir=out_dir)
    episodes = result["episodes"]
    by_task: dict[str, list[dict[str, Any]]] = {}
    for episode in episodes:
        by_task.setdefault(episode["task"], []).append(episode)
    first_success = sum(1 for values in by_task.values() if values and values[0]["reward"])
    final_success = sum(1 for values in by_task.values() if any(item["reward"] for item in values))
    latencies = [float(item.get("elapsed_seconds", 0.0)) for item in episodes]
    steps = [int(item.get("steps", 0)) for item in episodes]
    input_tokens = [int(item.get("input_tokens", 0)) for item in episodes]
    output_tokens = [int(item.get("output_tokens", 0)) for item in episodes]
    failures = Counter(
        str(item["failure_type"])
        for item in episodes
        if item.get("failure_type")
    )
    input_rate = float(getattr(cfg, "input_cost_per_million", 0.0))
    output_rate = float(getattr(cfg, "output_cost_per_million", 0.0))
    estimated_cost = sum(input_tokens) / 1_000_000 * input_rate + sum(output_tokens) / 1_000_000 * output_rate
    report: dict[str, Any] = {
        "schema_version": 1,
        "benchmark_version": "v1",
        "agentforge_version": __version__,
        "generated_at": time.time(),
        "duration_seconds": time.time() - started,
        "python": sys.version,
        "platform": platform.platform(),
        "model": cfg.model,
        "config": public_config(cfg),
        "task_order": [task.name for task in tasks],
        "seed": seed,
        "task_specs": [task.to_spec() for task in tasks],
        "summary": {
            "tasks": len(tasks),
            "episodes": len(episodes),
            "num_trials": num_trials,
            "k": k,
            "pass@1": result["pass@1"],
            "pass@k": result["pass@k"],
            "pass@3": result.get("pass@3"),
            "pass@5": result.get("pass@5"),
            "first_success_rate": first_success / len(tasks) if tasks else 0.0,
            "final_success_rate": final_success / len(tasks) if tasks else 0.0,
            "avg_steps": statistics.fmean(steps) if steps else 0.0,
            "avg_input_tokens": statistics.fmean(input_tokens) if input_tokens else 0.0,
            "avg_output_tokens": statistics.fmean(output_tokens) if output_tokens else 0.0,
            "estimated_cost": estimated_cost,
            "p50_latency_seconds": _percentile(latencies, 0.50),
            "p95_latency_seconds": _percentile(latencies, 0.95),
            "failure_types": dict(failures),
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
