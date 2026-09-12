from __future__ import annotations

import json
import sys

from agentforge.external_tasks import hash_directory, sha256_bytes
from scripts.prepare_external_dataset import prepare_dataset
from scripts.run_external_formal import build_matrix, matrix_sha256, run_formal


def _task(root, index: int, split: str) -> dict:
    task_id = f"repo-{index:02d}__issue-1"
    verifier = root / "verifier" / task_id
    verifier.mkdir(parents=True)
    (verifier / "check.py").write_text("raise SystemExit(1)\n", encoding="utf-8")
    return {
        "id": task_id,
        "dataset_version": "external-v1",
        "split": split,
        "repository": {
            "url": f"file:///repo-{index:02d}",
            "license": "MIT",
            "base_commit": "a" * 40,
            "source_url": f"https://example.invalid/repo-{index:02d}/issues/1",
        },
        "instruction": "Implement the requested behavior.",
        "category": "bug-fix",
        "difficulty": "medium",
        "allowed_paths": ["src/**"],
        "protected_paths": [".github/**"],
        "environment": {"python": "3.13", "install": [], "timeout_seconds": 30, "max_steps": 5},
        "verifier": {
            "path": f"verifier/{task_id}",
            "bundle_sha256": hash_directory(verifier),
            "commands": [[sys.executable, "{verifier_root}/check.py"]],
        },
        "reference": {
            "patch_sha256": sha256_bytes(b"reference"),
            "expected_changed_paths": ["src/module.py"],
        },
    }


def test_formal_runner_builds_frozen_900_item_matrix_and_missing_report(tmp_path):
    tasks = [_task(tmp_path, index, "validation" if index < 18 else "holdout") for index in range(30)]
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps({"schema_version": 1, "dataset_version": "external-v1", "tasks": tasks}),
        encoding="utf-8",
    )
    frozen = tmp_path / "frozen"
    prepare_dataset(manifest, frozen)

    from agentforge.external_tasks import ExternalDataset

    dataset = ExternalDataset.load(frozen)
    matrix = build_matrix(dataset, ["model-a", "model-b"])
    assert len(matrix) == 900
    assert matrix_sha256(matrix) == matrix_sha256(build_matrix(dataset, ["model-a", "model-b"]))

    output = tmp_path / "formal"
    report = run_formal(
        frozen,
        models=["model-a", "model-b"],
        base_reference_path=None,
        out_dir=output,
        execute=False,
    )
    assert report["ready"] is False
    assert report["planned_episodes"] == 900
    assert report["complete"] is False
    assert len(report["blockers"]) >= 2
    assert json.loads((output / "run-manifest.json").read_text())["matrix_sha256"] == matrix_sha256(matrix)
    missing = json.loads((output / "missing.json").read_text())
    assert missing["planned"] == 900
    assert missing["completed"] == 0
    assert len(missing["missing"]) == 900
