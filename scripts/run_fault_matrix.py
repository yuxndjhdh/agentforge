"""Run a versioned fault-injection smoke, pilot, or formal matrix."""

from __future__ import annotations

import argparse
import json
import platform
import subprocess
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agentforge.config import load_config
from agentforge.external_tasks import atomic_write_json, dependency_lock_sha256
from agentforge.fault_injection import (
    FAULT_FORMAL_DECLARED_TOTAL,
    FAULT_FORMAL_MATRIX_TOTAL,
    FAULT_PROTOCOL_VERSION,
    FAULT_SCENARIOS,
    FAULT_SUITE_VERSION,
    FaultTrial,
    fault_matrix_plan,
    fault_matrix_sha256,
    run_fault_trial,
    summarize_fault_trials,
)
from agentforge.sandbox import Sandbox


def _git_metadata() -> tuple[str | None, bool | None]:
    root = Path(__file__).resolve().parents[1]
    try:
        commit_result = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=root, capture_output=True, text=True, check=False
        )
        dirty_result = subprocess.run(
            ["git", "status", "--porcelain"], cwd=root, capture_output=True, text=True, check=False
        )
    except OSError:
        return None, None
    return (
        commit_result.stdout.strip() if commit_result.returncode == 0 else None,
        bool(dirty_result.stdout.strip()) if dirty_result.returncode == 0 else None,
    )


def _validate_existing(path: Path, expected: dict[str, Any]) -> tuple[FaultTrial | None, str | None]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        trial = FaultTrial.from_dict(raw)
    except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
        return None, f"cannot load existing trial: {type(exc).__name__}: {exc}"
    identity = {
        "scenario": trial.scenario,
        "trial": trial.trial,
        "seed": trial.seed,
        "expected": trial.expected,
        "trigger_event": trial.injection.get("trigger_event"),
        "action": trial.injection.get("action"),
    }
    mismatches = [key for key, value in expected.items() if identity.get(key) != value]
    if mismatches:
        return None, f"identity mismatch: {', '.join(mismatches)}"
    for key in ("trace", "events", "database", "side_effect_ledger"):
        relative = trial.artifacts.get(key)
        if not isinstance(relative, str) or not relative:
            continue
        artifact = (path.parent / relative).resolve()
        try:
            artifact.relative_to(path.parent.resolve())
        except ValueError:
            return None, f"artifact escapes trial directory: {relative}"
        if not artifact.is_file():
            # Older no-effect trials legitimately did not materialize an empty ledger.
            if key == "side_effect_ledger":
                continue
            return None, f"artifact is missing: {relative}"
    return trial, None


def _manifest_core(value: dict[str, Any]) -> dict[str, Any]:
    return {key: item for key, item in value.items() if key not in {"created_at", "updated_at"}}


def _load_or_write_manifest(path: Path, value: dict[str, Any]) -> None:
    if path.is_file():
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"fault run manifest is unreadable: {path}") from exc
        if not isinstance(existing, dict) or _manifest_core(existing) != _manifest_core(value):
            raise ValueError(f"fault run manifest does not match the requested matrix: {path}")
        return
    atomic_write_json(path, value)


def _resolve_scope(scope: str | None, trials: int | None) -> str:
    if scope:
        normalized = scope.lower()
        if normalized not in {"smoke", "pilot", "formal"}:
            raise ValueError("scope must be smoke, pilot, or formal")
        if trials is not None:
            expected = {"smoke": 1, "pilot": 5}.get(normalized)
            if expected is None or trials != expected:
                raise ValueError("--trials is only compatible with smoke=1 or pilot=5")
        return normalized
    if trials in {None, 1}:
        return "smoke"
    if trials == 5:
        return "pilot"
    raise ValueError("provide --scope smoke|pilot|formal; arbitrary trial counts are not supported")


def run_matrix(
    *,
    scope: str,
    out_dir: str | Path,
    seed: int,
    selected_names: list[str] | None = None,
    timeout_seconds: float = 20.0,
) -> dict[str, Any]:
    selected_set = set(selected_names or [])
    if scope == "formal" and selected_set:
        raise ValueError("formal scope does not allow a partial scenario selection")
    full_plan = fault_matrix_plan(scope, seed=seed)
    if selected_set - {item.name for item in FAULT_SCENARIOS}:
        raise ValueError("unknown fault scenario selection")
    plan = [item for item in full_plan if not selected_set or item["scenario"] in selected_set]
    if not plan:
        raise ValueError("fault matrix plan is empty")
    target = Path(out_dir).resolve()
    target.mkdir(parents=True, exist_ok=True)
    commit, dirty = _git_metadata()
    cfg = load_config()
    diagnostic = Sandbox.from_config(cfg).container_diagnostics()
    manifest = {
        "schema_version": 1,
        "suite_version": FAULT_SUITE_VERSION,
        "protocol_version": FAULT_PROTOCOL_VERSION,
        "formal_trial_total": FAULT_FORMAL_DECLARED_TOTAL,
        "scope": scope,
        "seed": seed,
        "plan": plan,
        "planned_trials": len(plan),
        "matrix_sha256": fault_matrix_sha256(plan),
        "git_commit": commit,
        "git_dirty": dirty,
        "python": sys.version,
        "platform": platform.platform(),
        "dependency_lock_sha256": dependency_lock_sha256(),
        "container_diagnostic": diagnostic,
        "protocol_consistent": scope != "formal" or len(plan) == FAULT_FORMAL_DECLARED_TOTAL,
        "protocol_warnings": (
            [
                f"scenario counts sum to {FAULT_FORMAL_MATRIX_TOTAL}, but the implementation plan declares {FAULT_FORMAL_DECLARED_TOTAL}; formal release is blocked until reconciled"
            ]
            if scope == "formal" and FAULT_FORMAL_MATRIX_TOTAL != FAULT_FORMAL_DECLARED_TOTAL
            else []
        ),
    }
    _load_or_write_manifest(target / "run-manifest.json", manifest)

    records: dict[tuple[str, int, int], FaultTrial] = {}
    invalid: list[dict[str, Any]] = []
    missing: list[dict[str, Any]] = []
    for item in plan:
        trial_root = target / item["scenario"] / str(item["trial"])
        path = trial_root / "trial.json"
        key = (item["scenario"], int(item["trial"]), int(item["seed"]))
        if path.is_file():
            trial, error = _validate_existing(path, item)
            if trial is not None:
                records[key] = trial
            else:
                invalid.append({"item": item, "path": str(path), "reason": error})
            continue
        missing.append(item)

    protocol_blocked = scope == "formal" and not manifest["protocol_consistent"]
    if not protocol_blocked:
        for item in list(missing):
            record = run_fault_trial(
                item["scenario"],
                trial=int(item["trial"]),
                seed=int(item["seed"]),
                out_dir=target,
                timeout_seconds=timeout_seconds,
            )
            key = (item["scenario"], int(item["trial"]), int(item["seed"]))
            records[key] = record
            missing.remove(item)

    ordered_records = [
        records[(item["scenario"], int(item["trial"]), int(item["seed"]))]
        for item in plan
        if (item["scenario"], int(item["trial"]), int(item["seed"])) in records
    ]
    summary = summarize_fault_trials(ordered_records)
    summary.update(
        {
            "scope": scope,
            "matrix_sha256": manifest["matrix_sha256"],
            "planned_trials": len(plan),
            "missing_trials": len(missing),
            "invalid_trial_records": invalid,
            "matrix_complete": (
                not missing
                and not invalid
                and len(ordered_records) == len(plan)
                and int(summary.get("invalid_trials", 0)) == 0
            ),
            "protocol_consistent": manifest["protocol_consistent"],
            "protocol_warnings": manifest["protocol_warnings"],
            "protocol_version": manifest["protocol_version"],
            "formal_trial_total": manifest["formal_trial_total"],
        }
    )
    summary["complete"] = bool(summary["matrix_complete"] and summary["protocol_consistent"])
    atomic_write_json(target / "summary.json", summary)
    atomic_write_json(
        target / "missing.json",
        {
            "schema_version": 1,
            "scope": scope,
            "matrix_sha256": manifest["matrix_sha256"],
            "planned": len(plan),
            "missing": missing,
            "invalid": invalid,
        },
    )
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run AgentForge fault-injection evidence")
    parser.add_argument("--scope", choices=("smoke", "pilot", "formal"), default=None)
    parser.add_argument("--out", default="runs/fault-injection/v1")
    parser.add_argument("--trials", type=int, default=None, help="legacy alias: only 1=smoke or 5=pilot")
    parser.add_argument("--scenario", action="append", choices=[item.name for item in FAULT_SCENARIOS])
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--timeout", type=float, default=20.0)
    args = parser.parse_args(argv)
    try:
        scope = _resolve_scope(args.scope, args.trials)
        summary = run_matrix(
            scope=scope,
            out_dir=args.out,
            seed=args.seed,
            selected_names=args.scenario,
            timeout_seconds=args.timeout,
        )
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if summary["complete"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
