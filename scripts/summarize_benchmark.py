"""Build the reproducible A/B/C/D benchmark comparison report."""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agentforge.benchmark import load_report

VARIANTS = ("A", "B", "C", "D")


def _number(value: Any, digits: int = 3) -> str:
    if value is None:
        return "unavailable"
    if isinstance(value, float):
        return f"{value:.{digits}f}"
    return str(value)


def _validate_report(path: Path, expected_variant: str) -> dict[str, Any]:
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
    required = {"reward", "trace_path", "attempts", "elapsed_seconds", "steps"}
    for index, episode in enumerate(episodes):
        if not required.issubset(episode):
            missing = sorted(required - set(episode))
            raise ValueError(f"{path}: episode {index} is missing {missing}")
    return report


def build_report(paths: dict[str, str | Path]) -> str:
    """Return deterministic Markdown generated only from four JSON reports."""
    reports = {
        variant: _validate_report(Path(paths[variant]), variant) for variant in VARIANTS
    }
    first = reports["A"]
    summary_rows = []
    for variant in VARIANTS:
        summary = reports[variant].get("summary") or {}
        summary_rows.append(
            "| {variant} | {pass1} | {pass3} | {pass5} | {first_success} | "
            "{final_success} | {latency} | {steps} | {input_tokens} | {output_tokens} | {cost} |".format(
                variant=variant,
                pass1=_number(summary.get("pass@1")),
                pass3=_number(summary.get("pass@3")),
                pass5=_number(summary.get("pass@5")),
                first_success=_number(summary.get("first_success_rate")),
                final_success=_number(summary.get("final_success_rate")),
                latency=f"{_number(summary.get('p50_latency_seconds'))}/"
                f"{_number(summary.get('p95_latency_seconds'))}",
                steps=_number(summary.get("avg_steps")),
                input_tokens=_number(summary.get("avg_input_tokens")),
                output_tokens=_number(summary.get("avg_output_tokens")),
                cost=_number(summary.get("estimated_cost"), 6),
            )
        )

    def delta(variant: str, field: str) -> str:
        base = (reports["A"].get("summary") or {}).get(field)
        value = (reports[variant].get("summary") or {}).get(field)
        if not isinstance(base, (int, float)) or not isinstance(value, (int, float)):
            return "unavailable"
        return _number(value - base)

    model = first.get("model", "unknown")
    git = first.get("git") or {}
    task_summary = first.get("task_summary") or {}
    return "\n".join(
        [
            "# Code Agent Benchmark Report",
            "",
            "This file is generated from the four variant `report.json` files. Do not edit metrics manually.",
            "",
            f"- Model: `{model}`",
            f"- AgentForge commit: `{git.get('commit') or 'unavailable'}`",
            f"- Workspace dirty: `{git.get('dirty')}`",
            f"- Tasks: `{task_summary.get('count', first.get('summary', {}).get('tasks', 'unknown'))}`",
            f"- Task order: `{', '.join(first.get('task_order', []))}`",
            "",
            "## Variant Configuration",
            "",
            "| Variant | Compression | Verify retry | Max attempts | Trials | Episodes |",
            "| --- | --- | --- | ---: | ---: | ---: |",
            *[
                "| {variant} | {compression} | {verify} | {attempts} | {trials} | {episodes} |".format(
                    variant=variant,
                    compression=bool((reports[variant].get("experiment") or {}).get("context_compression_enabled")),
                    verify=bool((reports[variant].get("experiment") or {}).get("verify_enabled")),
                    attempts=(reports[variant].get("experiment") or {}).get("max_attempts", "unavailable"),
                    trials=(reports[variant].get("summary") or {}).get("num_trials", "unavailable"),
                    episodes=(reports[variant].get("summary") or {}).get("episodes", "unavailable"),
                )
                for variant in VARIANTS
            ],
            "",
            "## Metrics",
            "",
            "| Variant | pass@1 | pass@3 | pass@5 | First success | Final success | p50/p95 latency | Avg steps | Avg input tokens | Avg output tokens | Cost |",
            "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
            *summary_rows,
            "",
            "## Ablation Deltas From A",
            "",
            "| Comparison | Final success delta | Avg input token delta | Estimated cost delta | Compression trigger rate | Verify incremental cost |",
            "| --- | ---: | ---: | ---: | ---: | ---: |",
            *[
                "| A -> {variant} | {success} | {tokens} | {cost} | {compression} | {verify_cost} |".format(
                    variant=variant,
                    success=delta(variant, "final_success_rate"),
                    tokens=delta(variant, "avg_input_tokens"),
                    cost=delta(variant, "estimated_cost"),
                    compression=_number((reports[variant].get("summary") or {}).get("compression_trigger_rate")),
                    verify_cost=_number((reports[variant].get("summary") or {}).get("verify_incremental_cost"), 6),
                )
                for variant in ("B", "C", "D")
            ],
            "",
            "## Limitations",
            "",
            "The comparison is descriptive for five trials per task; it is not a significance test. "
            "A compression or Verify conclusion requires the corresponding trigger/attempt data to be non-zero. "
            "Every metric above is sourced from the validated episode records in the four input reports.",
            "",
        ]
    )


def write_report(paths: dict[str, str | Path], output: str | Path) -> Path:
    target = Path(output)
    target.parent.mkdir(parents=True, exist_ok=True)
    content = build_report(paths)
    fd, temporary = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return target


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate the A/B/C/D benchmark Markdown report")
    for variant in VARIANTS:
        parser.add_argument(variant.lower(), type=Path, help=f"{variant} report.json")
    parser.add_argument("--out", type=Path, default=Path("docs/benchmark_report.md"))
    args = parser.parse_args()
    paths = {variant: getattr(args, variant.lower()) for variant in VARIANTS}
    try:
        target = write_report(paths, args.out)
    except (OSError, ValueError, TypeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(f"report: {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
