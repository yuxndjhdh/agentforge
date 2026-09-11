"""Generate a machine-readable Impact summary from benchmark reports.

The input reports remain the source of truth. This module only derives counts,
rates, comparisons, and provenance; it never accepts hand-entered metric
values.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agentforge.benchmark import load_report

VARIANTS = ("A", "B", "C", "D")


def _number(value: Any, *, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be numeric")
    return float(value)


def _rate(numerator: int, denominator: int) -> float:
    return numerator / denominator if denominator else 0.0


def _source_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _resolve_trace(path: str, repo_root: Path) -> Path:
    trace = Path(path)
    return trace if trace.is_absolute() else repo_root / trace


def _validate_report(path: Path, expected_variant: str, repo_root: Path) -> dict[str, Any]:
    report = load_report(path)
    if report.get("experiment_id") != expected_variant:
        raise ValueError(
            f"{path}: expected experiment_id {expected_variant!r}, "
            f"got {report.get('experiment_id')!r}"
        )
    experiment = report.get("experiment") or {}
    if experiment.get("experiment_id") != expected_variant:
        raise ValueError(f"{path}: experiment metadata does not match experiment_id")
    episodes = report.get("episodes")
    if not isinstance(episodes, list) or not episodes:
        raise ValueError(f"{path}: report has no episodes")
    required = {"reward", "first_reward", "trace_path", "attempts", "elapsed_seconds", "steps"}
    for index, episode in enumerate(episodes):
        if not isinstance(episode, dict) or not required.issubset(episode):
            missing = sorted(required - set(episode if isinstance(episode, dict) else {}))
            raise ValueError(f"{path}: episode {index} is missing {missing}")
        trace_path = _resolve_trace(str(episode["trace_path"]), repo_root)
        if not trace_path.is_file():
            raise ValueError(f"{path}: episode {index} trace is missing: {trace_path}")
    return report


def _episode_metrics(report: dict[str, Any]) -> dict[str, Any]:
    episodes = report["episodes"]
    count = len(episodes)
    first_successes = sum(int(item["first_reward"]) for item in episodes)
    final_successes = sum(int(item["reward"]) for item in episodes)
    first_failures = count - first_successes
    recovered = sum(
        1 for item in episodes if not int(item["first_reward"]) and int(item["reward"])
    )
    input_tokens = sum(int(item.get("input_tokens", 0)) for item in episodes)
    output_tokens = sum(int(item.get("output_tokens", 0)) for item in episodes)
    first_input_tokens = sum(int(item.get("first_input_tokens", 0)) for item in episodes)
    first_output_tokens = sum(int(item.get("first_output_tokens", 0)) for item in episodes)
    attempts = [int(item.get("attempt_count", 1)) for item in episodes]
    compression_count = sum(
        1 for item in episodes if int(item.get("compression_trigger_count", 0)) > 0
    )
    summary = report.get("summary") or {}
    config = report.get("config") or {}
    try:
        input_rate = float(config.get("input_cost_per_million", 0.0))
        output_rate = float(config.get("output_cost_per_million", 0.0))
    except (TypeError, ValueError):
        input_rate = output_rate = 0.0
    rates_available = input_rate > 0 and output_rate > 0
    estimated_cost = None
    if rates_available:
        estimated_cost = input_tokens / 1_000_000 * input_rate + output_tokens / 1_000_000 * output_rate

    return {
        "episodes": count,
        "first_attempt_successes": first_successes,
        "final_successes": final_successes,
        "first_attempt_failures": first_failures,
        "recovered_first_attempt_failures": recovered,
        "first_attempt_success_rate": _rate(first_successes, count),
        "final_success_rate": _rate(final_successes, count),
        "failure_recovery_rate": _rate(recovered, first_failures) if first_failures else None,
        "pass_at_1": summary.get("pass@1"),
        "pass_at_k": summary.get("pass@k"),
        "avg_attempts": sum(attempts) / count if count else 0.0,
        "avg_steps": sum(int(item.get("steps", 0)) for item in episodes) / count if count else 0.0,
        "total_input_tokens": input_tokens,
        "total_output_tokens": output_tokens,
        "avg_input_tokens": input_tokens / count if count else 0.0,
        "avg_output_tokens": output_tokens / count if count else 0.0,
        "first_attempt_input_tokens": first_input_tokens,
        "first_attempt_output_tokens": first_output_tokens,
        "verify_incremental_input_tokens": input_tokens - first_input_tokens,
        "verify_incremental_output_tokens": output_tokens - first_output_tokens,
        "compression_triggered_episodes": compression_count,
        "compression_trigger_rate": _rate(compression_count, count),
        "compression_saved_tokens": sum(int(item.get("compression_saved_tokens", 0)) for item in episodes),
        "p50_latency_seconds": summary.get("p50_latency_seconds"),
        "p95_latency_seconds": summary.get("p95_latency_seconds"),
        "cost": {
            "status": "estimated" if rates_available else "unavailable",
            "input_rate_per_million": input_rate if rates_available else None,
            "output_rate_per_million": output_rate if rates_available else None,
            "estimated_cost": estimated_cost,
        },
    }


def _difference(metrics: dict[str, Any], baseline: dict[str, Any], field: str) -> float | None:
    value = metrics.get(field)
    base = baseline.get(field)
    if not isinstance(value, (int, float)) or not isinstance(base, (int, float)):
        return None
    return float(value) - float(base)


def build_impact_summary(
    paths: dict[str, str | Path],
    *,
    repo_root: str | Path | None = None,
) -> dict[str, Any]:
    """Build an auditable summary from the four variant reports."""
    root = Path(repo_root or Path.cwd()).resolve()
    reports = {
        variant: _validate_report(Path(paths[variant]), variant, root) for variant in VARIANTS
    }
    first = reports["A"]
    expected_commit = first.get("git_commit")
    expected_model = first.get("model")
    expected_tasks = first.get("task_order")
    for variant, report in reports.items():
        if report.get("git_commit") != expected_commit:
            raise ValueError(f"{variant}: git commit differs from A")
        if report.get("model") != expected_model:
            raise ValueError(f"{variant}: model differs from A")
        if report.get("task_order") != expected_tasks:
            raise ValueError(f"{variant}: task order differs from A")

    metrics = {variant: _episode_metrics(report) for variant, report in reports.items()}
    c, d = metrics["C"], metrics["D"]
    c_input = c["avg_input_tokens"]
    compression_reduction = None
    if c_input:
        compression_reduction = (c_input - d["avg_input_tokens"]) / c_input

    source_reports = {
        variant: {
            "path": str(Path(paths[variant])),
            "sha256": _source_hash(Path(paths[variant])),
            "experiment_id": reports[variant]["experiment_id"],
            "git_commit": reports[variant].get("git_commit"),
            "workspace_dirty": reports[variant].get("workspace_dirty"),
        }
        for variant in VARIANTS
    }
    episode_total = sum(item["episodes"] for item in metrics.values())
    return {
        "schema_version": 1,
        "source": {
            "reports": source_reports,
            "model": expected_model,
            "git_commit": expected_commit,
            "task_count": len(expected_tasks or []),
            "task_order": expected_tasks,
            "episodes_total": episode_total,
            "description": "descriptive benchmark summary; not user impact evidence",
        },
        "variants": {
            variant: {
                "experiment": reports[variant].get("experiment", {}),
                "metrics": metrics[variant],
            }
            for variant in VARIANTS
        },
        "comparisons": {
            "verify_retry": {
                "baseline": "A",
                "variants": ["C", "D"],
                "formula": "recovered_first_attempt_failures / first_attempt_failures",
                "C": {
                    "first_attempt_success_rate": c["first_attempt_success_rate"],
                    "final_success_rate": c["final_success_rate"],
                    "absolute_final_minus_first": c["final_success_rate"] - c["first_attempt_success_rate"],
                    "recovered_first_attempt_failures": c["recovered_first_attempt_failures"],
                    "failure_recovery_rate": c["failure_recovery_rate"],
                },
                "D": {
                    "first_attempt_success_rate": d["first_attempt_success_rate"],
                    "final_success_rate": d["final_success_rate"],
                    "absolute_final_minus_first": d["final_success_rate"] - d["first_attempt_success_rate"],
                    "recovered_first_attempt_failures": d["recovered_first_attempt_failures"],
                    "failure_recovery_rate": d["failure_recovery_rate"],
                },
            },
            "context_compression": {
                "baseline": "C",
                "variant": "D",
                "formula": "(baseline_avg_input_tokens - variant_avg_input_tokens) / baseline_avg_input_tokens",
                "final_success_rate_delta": _difference(d, c, "final_success_rate"),
                "avg_input_tokens_delta": _difference(d, c, "avg_input_tokens"),
                "avg_output_tokens_delta": _difference(d, c, "avg_output_tokens"),
                "avg_input_token_reduction_rate": compression_reduction,
                "total_input_tokens_delta": _difference(d, c, "total_input_tokens"),
                "total_output_tokens_delta": _difference(d, c, "total_output_tokens"),
            },
        },
        "metric_definitions": {
            "first_attempt_success_rate": "count(first_reward == 1) / episode_count",
            "final_success_rate": "count(reward == 1) / episode_count",
            "failure_recovery_rate": "count(first_reward == 0 and reward == 1) / count(first_reward == 0)",
            "avg_input_tokens": "sum(input_tokens) / episode_count",
            "avg_output_tokens": "sum(output_tokens) / episode_count",
            "compression_trigger_rate": "count(compression_trigger_count > 0) / episode_count",
            "estimated_cost": "input_tokens / 1_000_000 * input_rate + output_tokens / 1_000_000 * output_rate",
        },
        "limitations": [
            "single model and fixed task set",
            "five trials per task; descriptive results, not a significance test",
            "not real-user Impact evidence",
            "cost is unavailable without positive supplier rates",
            "Docker benchmark evidence is not a complete container security proof",
        ],
    }


def write_summary(paths: dict[str, str | Path], output: str | Path, *, repo_root: str | Path | None = None) -> Path:
    target = Path(output)
    target.parent.mkdir(parents=True, exist_ok=True)
    content = build_impact_summary(paths, repo_root=repo_root)
    fd, temporary = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(content, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return target


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate a machine-readable Impact summary")
    for variant in VARIANTS:
        parser.add_argument(variant.lower(), type=Path, help=f"{variant} report.json")
    parser.add_argument("--out", type=Path, default=Path("docs/impact_summary.json"))
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    args = parser.parse_args()
    paths = {variant: getattr(args, variant.lower()) for variant in VARIANTS}
    try:
        target = write_summary(paths, args.out, repo_root=args.repo_root)
    except (OSError, ValueError, TypeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(f"summary: {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
