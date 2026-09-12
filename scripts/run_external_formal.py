"""Prepare or execute the frozen P1-C 900-episode matrix.

The formal runner is intentionally separate from the two-trial pilot.  It
requires a locked external-v1 dataset, two models, and base/reference
evidence before it can execute.  Each terminal episode is written separately
so an interrupted run can resume without repeating completed work.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any, Iterable

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agentforge.config import load_config
from agentforge.external_tasks import (
    ExternalDataset,
    ExternalTaskError,
    RepositoryMaterializer,
    atomic_write_json,
    audit_external_dataset,
    execution_metadata,
    run_external_episode,
    sha256_bytes,
    sha256_file,
    summarize_external_episodes,
)
from scripts.run_external_pilot import _load_base_reference

FORMAL_CONFIGS: tuple[dict[str, Any], ...] = (
    {"id": "E0", "verify_enabled": False, "context_compression_enabled": False, "max_attempts": 1},
    {"id": "E1", "verify_enabled": True, "context_compression_enabled": False, "max_attempts": 3},
    {"id": "E2", "verify_enabled": True, "context_compression_enabled": True, "max_attempts": 3},
)
FORMAL_TRIALS = 5
FORMAL_TASK_COUNT = 30
FORMAL_VALIDATION_COUNT = 18
FORMAL_HOLDOUT_COUNT = 12


def _safe_component(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", value).strip("._") or "item"


def episode_path(target: Path, item: dict[str, Any]) -> Path:
    return (
        target
        / "episodes"
        / _safe_component(str(item["model"]))
        / _safe_component(str(item["config"]))
        / _safe_component(str(item["task"]))
        / f"{int(item['trial'])}.json"
    )


def build_matrix(
    dataset: ExternalDataset,
    models: Iterable[str],
    *,
    seed: int = 0,
    trials: int = FORMAL_TRIALS,
) -> list[dict[str, Any]]:
    """Build a deterministic paired matrix with identical seeds per trial."""

    if trials != FORMAL_TRIALS:
        raise ExternalTaskError("P1-C is fixed at 5 trials per task/model/config")
    normalized = [model.strip() for model in models if model.strip()]
    if len(normalized) != 2 or len(set(normalized)) != 2:
        raise ExternalTaskError("P1-C requires two distinct model names")
    matrix: list[dict[str, Any]] = []
    for task in dataset.tasks:
        for model in normalized:
            for config in FORMAL_CONFIGS:
                for trial in range(trials):
                    matrix.append(
                        {
                            "task": task.id,
                            "split": task.split,
                            "model": model,
                            "config": str(config["id"]),
                            "trial": trial,
                            "seed": seed + trial,
                        }
                    )
    return matrix


def matrix_sha256(matrix: list[dict[str, Any]]) -> str:
    return sha256_bytes(json.dumps(matrix, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8"))


def _write(path: Path, value: object) -> None:
    atomic_write_json(path, value)


def _git_dirty() -> bool | None:
    metadata = execution_metadata(load_config())
    value = metadata.get("agentforge_dirty")
    return bool(value) if value is not None else None


def _preflight(
    dataset_path: str | Path,
    models: list[str],
    *,
    base_reference_path: str | Path | None,
    execute: bool,
) -> tuple[ExternalDataset, dict[str, Any], list[str]]:
    dataset = ExternalDataset.load(dataset_path)
    blockers: list[str] = []
    if dataset.version != "external-v1":
        blockers.append(f"P1-C requires dataset_version external-v1; found {dataset.version}")
    if not dataset.lock_verified:
        blockers.append(f"dataset lock is missing: {dataset.root / 'dataset.lock.json'}")
    if dataset.task_count != FORMAL_TASK_COUNT:
        blockers.append(f"P1-C requires exactly 30 tasks; found {dataset.task_count}")
    validation_count = len(dataset.by_split("validation"))
    holdout_count = len(dataset.by_split("holdout"))
    if validation_count != FORMAL_VALIDATION_COUNT:
        blockers.append(f"P1-C requires 18 validation tasks; found {validation_count}")
    if holdout_count != FORMAL_HOLDOUT_COUNT:
        blockers.append(f"P1-C requires 12 holdout tasks; found {holdout_count}")
    local_tasks = [task.id for task in dataset.tasks if task.is_local_fixture]
    if local_tasks:
        blockers.append(f"local file:// repositories are not valid P1-C evidence: {', '.join(local_tasks)}")
    normalized_models = [model.strip() for model in models if model.strip()]
    if len(normalized_models) != 2 or len(set(normalized_models)) != 2:
        blockers.append("P1-C requires two distinct model names")
    audit = audit_external_dataset(dataset)
    if audit["errors"]:
        blockers.extend(f"dataset audit: {error}" for error in audit["errors"])
    evidence_ok, evidence_error = _load_base_reference(base_reference_path, {task.id for task in dataset.tasks})
    if not evidence_ok and evidence_error:
        blockers.append(evidence_error)
    if execute:
        cfg = load_config()
        if not cfg.api_key:
            blockers.append("P1-C execution requires an LLM key from the environment; no key was found")
        if _git_dirty():
            blockers.append("P1-C execution requires a clean AgentForge working tree")
    return dataset, audit, blockers


def _manifest_core(manifest: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in manifest.items() if key not in {"created_at", "updated_at"}}


def _load_or_write_manifest(path: Path, manifest: dict[str, Any]) -> None:
    if path.is_file():
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ExternalTaskError(f"formal run manifest is unreadable: {path}") from exc
        if not isinstance(existing, dict) or _manifest_core(existing) != _manifest_core(manifest):
            raise ExternalTaskError(f"formal run manifest does not match the requested matrix: {path}")
        return
    _write(path, manifest)


def _load_episode(path: Path, expected: dict[str, Any]) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ExternalTaskError(f"existing formal episode is unreadable and will not be overwritten: {path}") from exc
    if not isinstance(value, dict):
        raise ExternalTaskError(f"existing formal episode is not an object: {path}")
    mismatches = [key for key, expected_value in expected.items() if value.get(key) != expected_value]
    required = ("reward", "first_reward", "verifier", "trace_path", "attempts", "terminal")
    missing = [key for key in required if key not in value]
    if mismatches or missing or value.get("terminal") is not True:
        detail = ", ".join(mismatches + missing) or "terminal=false"
        raise ExternalTaskError(f"existing formal episode failed validation and will not be overwritten: {path} ({detail})")
    return value


def _error_episode(item: dict[str, Any], error: Exception) -> dict[str, Any]:
    return {
        **item,
        "terminal": True,
        "reward": 0,
        "first_reward": 0,
        "verifier": {"passed": False, "checks": [], "elapsed_seconds": 0.0},
        "trace_path": None,
        "attempts": [],
        "failure_type": "runner_error",
        "error": f"{type(error).__name__}: {error}",
    }


def _missing_items(matrix: list[dict[str, Any]], completed: dict[tuple[Any, ...], dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        item
        for item in matrix
        if (item["task"], item["model"], item["config"], item["trial"], item["seed"]) not in completed
    ]


def run_formal(
    dataset_path: str | Path,
    *,
    models: list[str],
    base_reference_path: str | Path | None,
    out_dir: str | Path,
    seed: int = 0,
    execute: bool = False,
) -> dict[str, Any]:
    dataset, audit, blockers = _preflight(
        dataset_path,
        models,
        base_reference_path=base_reference_path,
        execute=execute,
    )
    matrix = build_matrix(dataset, models, seed=seed)
    target = Path(out_dir).resolve()
    target.mkdir(parents=True, exist_ok=True)
    cfg = load_config()
    lock_hash = sha256_file(dataset.lock_path) if dataset.lock_path is not None else None
    run_manifest = {
        "schema_version": 1,
        "scope": "external_formal",
        "dataset_version": dataset.version,
        "dataset_sha256": dataset.dataset_sha256,
        "manifest_sha256": dataset.manifest_sha256,
        "dataset_lock_sha256": lock_hash,
        "models": [model.strip() for model in models if model.strip()],
        "configurations": [dict(config) for config in FORMAL_CONFIGS],
        "trials": FORMAL_TRIALS,
        "seed": seed,
        "seed_list": [seed + trial for trial in range(FORMAL_TRIALS)],
        "planned_episodes": len(matrix),
        "matrix_sha256": matrix_sha256(matrix),
        "agentforge_commit": execution_metadata(cfg)["agentforge_commit"],
        "agentforge_dirty": execution_metadata(cfg)["agentforge_dirty"],
        "python": execution_metadata(cfg)["python"],
        "platform": execution_metadata(cfg)["platform"],
        "dependency_lock_sha256": execution_metadata(cfg)["dependency_lock_sha256"],
        "audit": audit,
    }
    _load_or_write_manifest(target / "run-manifest.json", run_manifest)

    completed: dict[tuple[Any, ...], dict[str, Any]] = {}
    invalid: list[dict[str, Any]] = []
    for item in matrix:
        path = episode_path(target, item)
        if not path.is_file():
            continue
        try:
            episode = _load_episode(path, item)
        except ExternalTaskError as exc:
            invalid.append({"item": item, "path": str(path), "error": str(exc)})
            continue
        completed[(item["task"], item["model"], item["config"], item["trial"], item["seed"])] = episode

    missing = _missing_items(matrix, completed)
    _write(
        target / "missing.json",
        {
            "schema_version": 1,
            "matrix_sha256": run_manifest["matrix_sha256"],
            "planned": len(matrix),
            "completed": len(completed),
            "missing": missing,
            "invalid": invalid,
        },
    )
    report: dict[str, Any] = {
        "schema_version": 1,
        "scope": "external_formal",
        "ready": not blockers,
        "executed": False,
        "complete": False,
        "dataset_version": dataset.version,
        "dataset_sha256": dataset.dataset_sha256,
        "manifest_sha256": dataset.manifest_sha256,
        "dataset_lock_sha256": lock_hash,
        "matrix_sha256": run_manifest["matrix_sha256"],
        "planned_episodes": len(matrix),
        "completed_episodes": len(completed),
        "missing_episodes": len(missing),
        "models": run_manifest["models"],
        "configurations": run_manifest["configurations"],
        "trials": FORMAL_TRIALS,
        "seed_list": run_manifest["seed_list"],
        "agentforge_commit": run_manifest["agentforge_commit"],
        "agentforge_dirty": run_manifest["agentforge_dirty"],
        "python": run_manifest["python"],
        "platform": run_manifest["platform"],
        "dependency_lock_sha256": run_manifest["dependency_lock_sha256"],
        "audit": audit,
        "blockers": blockers,
        "invalid": invalid,
        "episodes": [],
        "limitations": [
            "Formal P1-C requires real public repositories, two models, and manual audit evidence.",
            "Task-level bootstrap treats tasks, not repeated trials, as the resampling unit.",
        ],
    }
    if blockers or not execute:
        _write(target / "report.json", report)
        _write(target / "summary.json", {"scope": "external_formal", "complete": False, "missing": missing, "invalid": invalid})
        return report

    materializer = RepositoryMaterializer(target / "cache")
    task_index = {task.id: task for task in dataset.tasks}
    config_index = {str(config["id"]): config for config in FORMAL_CONFIGS}
    episodes = list(completed.values())
    for item in matrix:
        key = (item["task"], item["model"], item["config"], item["trial"], item["seed"])
        if key in completed:
            continue
        task = task_index[item["task"]]
        config = config_index[item["config"]]
        current_cfg = replace(
            cfg,
            model=item["model"],
            context_compression_enabled=bool(config["context_compression_enabled"]),
        )
        try:
            episode = run_external_episode(
                current_cfg,
                task,
                materializer=materializer,
                out_dir=target / "traces" / item["model"] / item["config"],
                verify_enabled=bool(config["verify_enabled"]),
                max_attempts=int(config["max_attempts"]),
                model_name=item["model"],
                config_id=item["config"],
                config_metadata=config,
                trial=int(item["trial"]),
                seed=int(item["seed"]),
                dataset_sha256=dataset.dataset_sha256,
                dataset_lock_sha256=lock_hash,
            )
            episode["terminal"] = True
        except Exception as exc:
            episode = _error_episode(item, exc)
        path = episode_path(target, item)
        _write(path, episode)
        completed[key] = episode
        episodes.append(episode)
        _write(
            target / "missing.json",
            {
                "schema_version": 1,
                "matrix_sha256": run_manifest["matrix_sha256"],
                "planned": len(matrix),
                "completed": len(completed),
                "missing": _missing_items(matrix, completed),
                "invalid": invalid,
            },
        )

    missing = _missing_items(matrix, completed)
    episodes = [completed[(item["task"], item["model"], item["config"], item["trial"], item["seed"])] for item in matrix if (item["task"], item["model"], item["config"], item["trial"], item["seed"]) in completed]
    summary = summarize_external_episodes(episodes, seed=seed)
    report.update(
        {
            "executed": True,
            "complete": not missing and not invalid and len(completed) == len(matrix),
            "completed_episodes": len(completed),
            "missing_episodes": len(missing),
            "missing": missing,
            "episodes": episodes,
            "summary": summary,
        }
    )
    _write(target / "missing.json", {"schema_version": 1, "matrix_sha256": run_manifest["matrix_sha256"], "planned": len(matrix), "completed": len(completed), "missing": missing, "invalid": invalid})
    _write(target / "summary.json", summary)
    _write(target / "report.json", report)
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Prepare or execute the P1-C external formal matrix")
    parser.add_argument("--dataset", required=True, help="locked external-v1 dataset manifest or directory")
    parser.add_argument("--models", required=True, help="two comma-separated model names")
    parser.add_argument("--base-reference", default=None, help="JSON evidence containing base_reference rows")
    parser.add_argument("--out", default="runs/external-benchmark/external-v1-formal")
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--execute", action="store_true", help="run model episodes after all preflight checks pass")
    args = parser.parse_args(argv)
    try:
        report = run_formal(
            args.dataset,
            models=args.models.split(","),
            base_reference_path=args.base_reference,
            out_dir=args.out,
            seed=args.seed,
            execute=args.execute,
        )
    except (ExternalTaskError, OSError, ValueError) as exc:
        parser.error(str(exc))
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["ready"] and (not args.execute or report["complete"]) else 2


if __name__ == "__main__":
    raise SystemExit(main())
