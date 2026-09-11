"""Prepare or execute the P1-B external 120-episode pilot.

The command is deliberately strict about prerequisites. Without a real
10-task validation manifest, base/reference evidence, two model names, and an
LLM key, it writes a blocked preflight report instead of substituting local
fixtures or silently reducing the requested matrix.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agentforge.config import load_config
from agentforge.external_tasks import (
    ExternalDataset,
    ExternalTaskError,
    RepositoryMaterializer,
    audit_external_dataset,
    run_external_episode,
    summarize_external_episodes,
)

PILOT_CONFIGS: tuple[dict[str, Any], ...] = (
    {
        "id": "E0",
        "verify_enabled": False,
        "context_compression_enabled": False,
        "max_attempts": 1,
    },
    {
        "id": "E1",
        "verify_enabled": True,
        "context_compression_enabled": False,
        "max_attempts": 3,
    },
    {
        "id": "E2",
        "verify_enabled": True,
        "context_compression_enabled": True,
        "max_attempts": 3,
    },
)


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _load_base_reference(path: str | Path | None, task_ids: set[str]) -> tuple[bool, str | None]:
    if path is None:
        return False, "base/reference evidence report was not supplied"
    source = Path(path)
    if not source.is_file():
        return False, f"base/reference evidence report does not exist: {source}"
    try:
        raw = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return False, f"cannot read base/reference evidence report: {source}: {exc}"
    rows = raw.get("base_reference") if isinstance(raw, dict) else raw
    if not isinstance(rows, list):
        return False, "base/reference evidence must be a list or an object with base_reference"
    indexed = {str(row.get("task")): row for row in rows if isinstance(row, dict)}
    missing = sorted(task_ids - set(indexed))
    if missing:
        return False, f"base/reference evidence is missing tasks: {', '.join(missing)}"
    invalid = sorted(
        task_id
        for task_id in task_ids
        if not (indexed[task_id].get("base_passed") is False and indexed[task_id].get("reference_passed") is True)
    )
    if invalid:
        return False, f"base/reference evidence is not fail/pass for: {', '.join(invalid)}"
    return True, None


def preflight(
    dataset_path: str | Path,
    models: list[str],
    *,
    base_reference_path: str | Path | None,
    execute: bool,
) -> tuple[ExternalDataset, dict[str, Any], list[str]]:
    dataset = ExternalDataset.load(dataset_path, split="validation")
    audit = audit_external_dataset(dataset)
    blockers: list[str] = []
    if len(dataset.tasks) != 10:
        blockers.append(f"P1-B requires exactly 10 validation tasks; found {len(dataset.tasks)}")
    local_tasks = [task.id for task in dataset.tasks if task.is_local_fixture]
    if local_tasks:
        blockers.append(f"local file:// repositories are not valid P1-B evidence: {', '.join(local_tasks)}")
    distinct_models = [model.strip() for model in models if model.strip()]
    if len(distinct_models) != 2 or len(set(distinct_models)) != 2:
        blockers.append("P1-B requires two distinct model names")
    if audit["errors"]:
        blockers.extend(f"dataset audit: {error}" for error in audit["errors"])
    evidence_ok, evidence_error = _load_base_reference(base_reference_path, {task.id for task in dataset.tasks})
    if not evidence_ok and evidence_error:
        blockers.append(evidence_error)
    cfg = load_config()
    if execute and not cfg.api_key:
        blockers.append("P1-B execution requires an LLM key from the environment; no key was found")
    return dataset, audit, blockers


def run_pilot(
    dataset_path: str | Path,
    *,
    models: list[str],
    base_reference_path: str | Path | None,
    out_dir: str | Path,
    trials: int = 2,
    seed: int = 0,
    execute: bool = False,
) -> dict[str, Any]:
    if trials != 2:
        raise ExternalTaskError("P1-B is fixed at 2 trials per task/model/config")
    dataset, audit, blockers = preflight(
        dataset_path,
        models,
        base_reference_path=base_reference_path,
        execute=execute,
    )
    target = Path(out_dir).resolve()
    target.mkdir(parents=True, exist_ok=True)
    report: dict[str, Any] = {
        "schema_version": 1,
        "scope": "pilot_preflight",
        "ready": not blockers,
        "executed": False,
        "dataset_version": dataset.version,
        "dataset_sha256": dataset.dataset_sha256,
        "manifest_sha256": dataset.manifest_sha256,
        "tasks": len(dataset.tasks),
        "models": [model.strip() for model in models if model.strip()],
        "configurations": [dict(item) for item in PILOT_CONFIGS],
        "requested_episodes": len(dataset.tasks) * 2 * len(PILOT_CONFIGS) * trials,
        "audit": audit,
        "blockers": blockers,
        "episodes": [],
        "limitations": [
            "A preflight report is not a benchmark result.",
            "Formal P1-B requires real public repositories and two configured models.",
        ],
    }
    if blockers or not execute:
        _write_json(target / "report.json", report)
        return report

    base_cfg = load_config()
    materializer = RepositoryMaterializer(target / "cache")
    episodes: list[dict[str, Any]] = []
    for model in report["models"]:
        for config in PILOT_CONFIGS:
            cfg = replace(
                base_cfg,
                model=model,
                context_compression_enabled=bool(config["context_compression_enabled"]),
            )
            for trial in range(trials):
                for task in dataset.tasks:
                    episodes.append(
                        run_external_episode(
                            cfg,
                            task,
                            materializer=materializer,
                            out_dir=target / "traces" / model / str(config["id"]),
                            verify_enabled=bool(config["verify_enabled"]),
                            max_attempts=int(config["max_attempts"]),
                            model_name=model,
                            config_id=str(config["id"]),
                            config_metadata=config,
                            trial=trial,
                            seed=seed + trial,
                            dataset_sha256=dataset.dataset_sha256,
                        )
                    )
    report["scope"] = "external_pilot"
    report["executed"] = True
    report["episodes"] = episodes
    report["summary"] = summarize_external_episodes(episodes, seed=seed)
    _write_json(target / "report.json", report)
    _write_json(target / "episodes.json", episodes)
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Preflight or execute the P1-B external pilot")
    parser.add_argument("--dataset", required=True, help="frozen external dataset manifest or directory")
    parser.add_argument("--models", required=True, help="two comma-separated model names")
    parser.add_argument("--base-reference", default=None, help="JSON evidence containing base_reference rows")
    parser.add_argument("--out", default="runs/external-benchmark/p1b-pilot")
    parser.add_argument("--trials", type=int, default=2)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--execute", action="store_true", help="run model episodes after all preflight checks pass")
    args = parser.parse_args(argv)
    try:
        report = run_pilot(
            args.dataset,
            models=args.models.split(","),
            base_reference_path=args.base_reference,
            out_dir=args.out,
            trials=args.trials,
            seed=args.seed,
            execute=args.execute,
        )
    except (ExternalTaskError, OSError, ValueError) as exc:
        parser.error(str(exc))
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["ready"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
