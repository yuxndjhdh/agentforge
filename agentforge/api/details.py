"""Run detail projections used by the API workbench and export endpoint."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any, Iterable


def _status(value: Any) -> str:
    return str(getattr(value, "value", value))


def _token_usage(step: Any) -> tuple[int, int]:
    metadata = getattr(step, "metadata", {}) or {}
    raw = metadata.get("raw") if isinstance(metadata, dict) else None
    if not isinstance(raw, dict):
        raw = metadata if isinstance(metadata, dict) else {}
    usage = raw.get("token_usage") or {}
    try:
        return int(usage.get("input") or 0), int(usage.get("output") or 0)
    except (TypeError, ValueError):
        return 0, 0


def serialize_checkpoints(checkpoints: Iterable[Any]) -> list[dict[str, Any]]:
    return [
        {
            "sequence": checkpoint.sequence,
            "attempt_id": checkpoint.attempt_id,
            "created_at": checkpoint.created_at,
            "complete": checkpoint.complete,
            "schema_version": checkpoint.schema_version,
        }
        for checkpoint in checkpoints
    ]


def summarize_run(
    run: Any,
    attempts: list[Any],
    steps: list[Any],
    tool_calls: list[Any],
    verifications: list[Any],
    checkpoints: list[Any],
    events: list[dict[str, Any]],
    *,
    config: dict[str, Any] | None = None,
    trace: dict[str, Any] | None = None,
) -> dict[str, Any]:
    input_tokens = 0
    output_tokens = 0
    for step in steps:
        input_count, output_count = _token_usage(step)
        input_tokens += input_count
        output_tokens += output_count

    timestamps = [
        value
        for attempt in attempts
        for value in (attempt.started_at, attempt.finished_at)
        if isinstance(value, (int, float))
    ]
    wall_time = max(timestamps) - min(timestamps) if len(timestamps) >= 2 else 0.0
    passed_verifications = sum(1 for item in verifications if item.reward == 1)
    successful_tools = sum(1 for item in tool_calls if _status(item.status) == "succeeded")
    failed_tools = sum(1 for item in tool_calls if _status(item.status) == "failed")
    compression_stats = (trace or {}).get("compression_stats") or []
    compression_saved_tokens = sum(
        int(item.get("saved_tokens", 0))
        for item in compression_stats
        if isinstance(item, dict)
    )
    input_rate = float((config or {}).get("input_cost_per_million", 0) or 0)
    output_rate = float((config or {}).get("output_cost_per_million", 0) or 0)
    estimated_cost = None
    cost_status = "unavailable"
    if input_rate > 0 and output_rate > 0:
        estimated_cost = input_tokens / 1_000_000 * input_rate + output_tokens / 1_000_000 * output_rate
        cost_status = "estimated"
    cancel_requested = bool(getattr(run, "cancel_requested", False)) or any(
        event.get("type") == "run.cancel_requested" for event in events
    )
    cancel_completed = any(event.get("type") == "run.cancelled" for event in events)

    return {
        "attempt_count": len(attempts),
        "step_count": len(steps),
        "tool_call_count": len(tool_calls),
        "tool_calls_succeeded": successful_tools,
        "tool_calls_failed": failed_tools,
        "verification_count": len(verifications),
        "verification_passed": passed_verifications,
        "checkpoint_count": len(checkpoints),
        "latest_checkpoint_sequence": checkpoints[-1].sequence if checkpoints else None,
        "event_count": len(events),
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": input_tokens + output_tokens,
        "wall_time_seconds": max(0.0, wall_time),
        "compression_requests": len(compression_stats),
        "compression_saved_tokens": compression_saved_tokens,
        "cost_status": cost_status,
        "estimated_cost": estimated_cost,
        "cancellation": {
            "requested": cancel_requested,
            "completed": cancel_completed,
            "reclaimed": cancel_completed and _status(run.status) == "cancelled",
        },
    }


def workspace_diff(workdir: str, *, max_chars: int = 200_000) -> dict[str, Any]:
    """Return a bounded git diff without invoking a shell."""
    path = Path(workdir)
    if not path.is_dir():
        return {"available": False, "reason": "workspace is not a directory", "text": "", "files": []}
    env = dict(os.environ)
    for name in list(env):
        upper = name.upper()
        if any(secret in upper for secret in ("API_KEY", "TOKEN", "SECRET", "PASSWORD")):
            del env[name]
    try:
        result = subprocess.run(
            ["git", "diff", "--no-ext-diff", "--no-color", "--no-renames"],
            cwd=path,
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"available": False, "reason": f"git diff unavailable: {type(exc).__name__}", "text": "", "files": []}
    if result.returncode != 0:
        return {"available": False, "reason": "workspace is not a git repository", "text": "", "files": []}
    text = result.stdout
    files = []
    for line in text.splitlines():
        if line.startswith("diff --git a/"):
            fields = line.split(" ")
            if len(fields) >= 4:
                files.append(fields[3][2:])
    truncated = len(text) > max_chars
    return {
        "available": True,
        "reason": None,
        "text": text[:max_chars],
        "files": list(dict.fromkeys(files)),
        "truncated": truncated,
    }


def run_config(run: Any, fallback: dict[str, Any]) -> dict[str, Any]:
    metadata = getattr(run, "metadata", {}) or {}
    configured = metadata.get("config") if isinstance(metadata, dict) else None
    return dict(configured) if isinstance(configured, dict) else dict(fallback)


def checkpoint_status(checkpoints: list[Any]) -> dict[str, Any]:
    latest = checkpoints[-1] if checkpoints else None
    return {
        "exists": bool(checkpoints),
        "count": len(checkpoints),
        "latest_sequence": latest.sequence if latest else None,
        "latest_complete": bool(latest.complete) if latest else False,
        "latest_attempt_id": latest.attempt_id if latest else None,
    }
