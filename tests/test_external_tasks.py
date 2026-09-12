from __future__ import annotations

import json
import subprocess
import sys

import pytest

from agentforge.external_tasks import (
    ExternalDataset,
    ExternalTaskError,
    RepositoryMaterializer,
    audit_external_dataset,
    hash_directory,
    sha256_bytes,
    summarize_external_episodes,
)
from scripts.prepare_external_dataset import prepare_dataset
from scripts.run_external_pilot import preflight


def _manifest_task(root, task_id: str, split: str, repository: str) -> dict:
    verifier = root / "verifier" / task_id
    verifier.mkdir(parents=True)
    (verifier / "check.py").write_text("print('check')\n", encoding="utf-8")
    return {
        "id": task_id,
        "dataset_version": "external-test-1",
        "split": split,
        "repository": {
            "url": repository,
            "license": "MIT",
            "base_commit": "a" * 40,
            "source_url": "https://example.invalid/issues/1",
        },
        "instruction": "Implement the requested behavior without changing protected files.",
        "category": "bug-fix",
        "difficulty": "medium",
        "allowed_paths": ["src/**"],
        "protected_paths": [".github/**", "external-verifier/**"],
        "environment": {"python": "3.12", "install": [], "timeout_seconds": 30, "max_steps": 5},
        "verifier": {
            "path": f"verifier/{task_id}",
            "bundle_sha256": hash_directory(verifier),
            "commands": [[sys.executable, "-c", "raise SystemExit(0)"]],
        },
        "reference": {
            "patch_sha256": sha256_bytes(b"reference patch"),
            "expected_changed_paths": ["src/module.py"],
        },
    }


def test_external_dataset_loads_and_hashes_verifier(tmp_path):
    raw = {
        "schema_version": 1,
        "dataset_version": "external-test-1",
        "tasks": [_manifest_task(tmp_path, "repo-a__issue-1", "validation", "file:///repo-a")],
    }
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(raw), encoding="utf-8")

    dataset = ExternalDataset.load(path)
    assert dataset.version == "external-test-1"
    assert dataset.tasks[0].verifier.bundle_sha256 == hash_directory(tmp_path / "verifier/repo-a__issue-1")
    assert dataset.tasks[0].changed_paths_allowed(["src/module.py"]) == (True, [])
    assert dataset.tasks[0].changed_paths_allowed([".github/workflows/ci.yml"]) == (
        False,
        [".github/workflows/ci.yml"],
    )

    audit = audit_external_dataset(dataset)
    assert audit["summary"]["valid_tasks"] == 1
    assert audit["summary"]["external_validity_evidence"] is False
    assert audit["warnings"]

    frozen = tmp_path / "frozen"
    prepared = prepare_dataset(path, frozen)
    assert prepared["tasks"] == 1
    assert ExternalDataset.load(frozen).tasks[0].verifier.root.is_dir()


def test_external_dataset_lock_rejects_tampering(tmp_path):
    task = _manifest_task(tmp_path, "repo-a__issue-1", "validation", "file:///repo-a")
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"dataset_version": "external-test-1", "tasks": [task]}), encoding="utf-8")
    frozen = tmp_path / "frozen"
    prepare_dataset(manifest, frozen)

    lock = json.loads((frozen / "dataset.lock.json").read_text(encoding="utf-8"))
    lock["manifest_sha256"] = "0" * 64
    (frozen / "dataset.lock.json").write_text(json.dumps(lock), encoding="utf-8")
    with pytest.raises(ExternalTaskError, match="dataset lock mismatch"):
        ExternalDataset.load(frozen)


def test_external_audit_runs_base_and_reference_verifiers(tmp_path):
    repository = tmp_path / "repo"
    repository.mkdir()
    subprocess.run(["git", "init"], cwd=repository, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=repository, check=True)
    subprocess.run(["git", "config", "user.name", "AgentForge Test"], cwd=repository, check=True)
    (repository / "src").mkdir()
    (repository / "src" / "value.txt").write_text("base\n", encoding="utf-8")
    subprocess.run(["git", "add", "src/value.txt"], cwd=repository, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "base"], cwd=repository, check=True, capture_output=True)
    base = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repository, text=True).strip()
    (repository / "src" / "value.txt").write_text("fixed\n", encoding="utf-8")
    subprocess.run(["git", "add", "src/value.txt"], cwd=repository, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "reference"], cwd=repository, check=True, capture_output=True)
    reference = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repository, text=True).strip()
    patch = subprocess.check_output(["git", "diff", base, reference], cwd=repository)

    task = _manifest_task(tmp_path, "repo-a__issue-1", "validation", repository.as_uri())
    verifier = tmp_path / "verifier" / "repo-a__issue-1"
    (verifier / "check.py").write_text(
        "from pathlib import Path\n"
        "raise SystemExit(0 if Path('src/value.txt').read_text() == 'fixed\\n' else 1)\n",
        encoding="utf-8",
    )
    task["verifier"]["path"] = "verifier/repo-a__issue-1"
    task["verifier"]["commands"] = [[sys.executable, "{verifier_root}/check.py"]]
    task["verifier"]["bundle_sha256"] = hash_directory(verifier)
    task["repository"]["base_commit"] = base
    task["reference"] = {
        "patch_sha256": sha256_bytes(patch),
        "expected_changed_paths": ["src/value.txt"],
        "reference_commit": reference,
    }
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"dataset_version": "external-test-1", "tasks": [task]}), encoding="utf-8")
    dataset = ExternalDataset.load(manifest)
    report = audit_external_dataset(
        dataset,
        check_repositories=True,
        execute_repository_checks=True,
        require_base_reference=True,
        materializer=RepositoryMaterializer(tmp_path / "cache"),
    )
    item = report["tasks"][0]
    assert report["errors"] == []
    assert item["base_checked"] is True
    assert item["base_passed"] is False
    assert item["reference_checked"] is True
    assert item["reference_passed"] is True
    assert item["reference_changed_paths"] == ["src/value.txt"]


def test_external_dataset_rejects_repository_split_leakage(tmp_path):
    first = _manifest_task(tmp_path, "repo-a__issue-1", "validation", "https://github.com/example/repo.git")
    second = _manifest_task(tmp_path, "repo-a__issue-2", "holdout", "https://github.com/example/repo.git")
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps({"dataset_version": "external-test-1", "tasks": [first, second]}), encoding="utf-8")
    with pytest.raises(ExternalTaskError, match="crosses validation/holdout"):
        ExternalDataset.load(path)


def test_external_summary_uses_task_bootstrap_and_explicit_denominators():
    episodes = [
        {"task": "a", "reward": 1, "first_reward": 0},
        {"task": "a", "reward": 1, "first_reward": 1},
        {"task": "b", "reward": 0, "first_reward": 0},
        {"task": "b", "reward": 0, "first_reward": 0},
    ]
    summary = summarize_external_episodes(episodes, seed=4)
    assert summary["final_success"] == {"numerator": 2, "denominator": 4, "rate": 0.5}
    assert summary["tasks"] == 2
    assert summary["summary_scope"] == "descriptive_only"
    assert len(summary["task_level_final_rate"]["bootstrap_95_ci"]) == 2


def test_external_pilot_preflight_refuses_small_local_fixture(tmp_path):
    task = _manifest_task(tmp_path, "repo-a__issue-1", "validation", "file:///repo-a")
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps({"dataset_version": "external-test-1", "tasks": [task]}),
        encoding="utf-8",
    )
    _, audit, blockers = preflight(
        manifest,
        ["model-a", "model-b"],
        base_reference_path=None,
        execute=False,
    )
    assert audit["summary"]["valid_tasks"] == 1
    assert any("exactly 10 validation tasks" in item for item in blockers)
    assert any("local file://" in item for item in blockers)
    assert any("base/reference" in item for item in blockers)
