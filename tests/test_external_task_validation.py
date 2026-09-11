from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from agentforge.external_tasks import (
    ExternalDataset,
    ExternalTaskError,
    ExternalTaskSpec,
    VerifierBundle,
    audit_external_dataset,
    bootstrap_task_ci,
    hash_directory,
    sha256_bytes,
    sha256_file,
    summarize_external_episodes,
    workspace_files,
)

TASK_ID = "repo-a__issue-1"
DATASET_VERSION = "external-test-1"


def _task(
    root: Path,
    task_id: str = TASK_ID,
    *,
    split: str = "validation",
    repository: str = "https://github.com/example/repo.git",
) -> dict:
    verifier = root / "verifier" / task_id
    verifier.mkdir(parents=True, exist_ok=True)
    (verifier / "check.py").write_text("print('check')\n", encoding="utf-8")
    return {
        "id": task_id,
        "dataset_version": DATASET_VERSION,
        "split": split,
        "repository": {
            "url": repository,
            "license": "MIT",
            "base_commit": "a" * 40,
            "source_url": "https://example.invalid/issues/1",
        },
        "instruction": "Implement the requested behavior without touching protected files.",
        "category": "bug-fix",
        "difficulty": "medium",
        "allowed_paths": ["src/**"],
        "protected_paths": [".github/**"],
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


def _write_manifest(root: Path, tasks: list[dict], version: str = DATASET_VERSION) -> Path:
    path = root / "manifest.json"
    path.write_text(json.dumps({"dataset_version": version, "tasks": tasks}), encoding="utf-8")
    return path


def test_hash_helpers_are_content_addressed(tmp_path):
    payload = tmp_path / "payload.bin"
    payload.write_bytes(b"agentforge")
    assert sha256_file(payload) == sha256_bytes(b"agentforge")

    baseline = hash_directory(tmp_path)
    (tmp_path / "extra.txt").write_text("x", encoding="utf-8")
    assert hash_directory(tmp_path) != baseline


def test_hash_directory_rejects_missing_bundle(tmp_path):
    with pytest.raises(ExternalTaskError, match="does not exist"):
        hash_directory(tmp_path / "missing")


def test_hash_directory_rejects_symlinked_bundle(tmp_path):
    target = tmp_path / "target.txt"
    target.write_text("x", encoding="utf-8")
    try:
        (tmp_path / "link.txt").symlink_to(target)
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation is not permitted on this platform")
    with pytest.raises(ExternalTaskError, match="symlink"):
        hash_directory(tmp_path)


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        ("not-an-object", "verifier must be an object"),
        ({"bundle_sha256": "short"}, "bundle_sha256 must be a lowercase SHA-256"),
        ({"bundle_sha256": "b" * 64, "commands": []}, r"commands must be a non-empty list"),
        ({"bundle_sha256": "b" * 64, "commands": [[]]}, r"commands\[0\] must be a non-empty argv list"),
        ({"bundle_sha256": "b" * 64, "commands": [["python", ""]]}, r"commands\[0\] must be a non-empty argv list"),
        (
            {"bundle_sha256": "b" * 64, "commands": [["python"]], "timeout_seconds": "soon"},
            "timeout_seconds must be numeric",
        ),
        (
            {"bundle_sha256": "b" * 64, "commands": [["python"]], "timeout_seconds": 0},
            "timeout_seconds must be between 0 and 3600",
        ),
        ({"bundle_sha256": "b" * 64, "commands": [["python"]], "path": 7}, "path must be a relative string"),
    ],
)
def test_verifier_bundle_from_manifest_rejects_untrusted_input(tmp_path, raw, message):
    with pytest.raises(ExternalTaskError, match=message):
        VerifierBundle.from_manifest(raw, dataset_root=tmp_path, task_id=TASK_ID)


def test_verifier_bundle_from_manifest_rejects_path_outside_dataset_root(tmp_path):
    raw = {
        "bundle_sha256": "b" * 64,
        "commands": [["python"]],
        "path": str(tmp_path.parent),
    }
    with pytest.raises(ExternalTaskError, match="escapes the dataset root"):
        VerifierBundle.from_manifest(raw, dataset_root=tmp_path, task_id=TASK_ID)


def test_verifier_bundle_validate_enforces_hash_and_workspace_isolation(tmp_path):
    verifier = tmp_path / "verifier"
    verifier.mkdir()
    (verifier / "check.py").write_text("print('check')\n", encoding="utf-8")

    bundle = VerifierBundle(
        root=verifier,
        bundle_sha256=hash_directory(verifier),
        commands=((sys.executable, "-c", "raise SystemExit(0)"),),
        timeout_seconds=30.0,
    )
    assert bundle.validate() == []
    with pytest.raises(ExternalTaskError, match="must not be inside the agent workspace"):
        bundle.validate(agent_workspace=tmp_path)

    (verifier / "check.py").write_text("print('tampered')\n", encoding="utf-8")
    with pytest.raises(ExternalTaskError, match="hash mismatch"):
        bundle.validate()

    missing = VerifierBundle(
        root=tmp_path / "absent",
        bundle_sha256="c" * 64,
        commands=(("true",),),
        timeout_seconds=5.0,
    )
    with pytest.raises(ExternalTaskError, match="is missing"):
        missing.validate()


def test_verifier_bundle_expands_tokens_and_runs_commands(tmp_path):
    verifier = tmp_path / "verifier"
    verifier.mkdir()
    (verifier / "check.py").write_text("print('ok')\n", encoding="utf-8")
    workdir = tmp_path / "agent-work"
    workdir.mkdir()

    bundle = VerifierBundle(
        root=verifier,
        bundle_sha256=hash_directory(verifier),
        commands=(
            (sys.executable, "-c", "raise SystemExit(0)"),
            (sys.executable, "-c", "raise SystemExit(3)"),
        ),
        timeout_seconds=30.0,
    )

    expanded = bundle.expand_command(
        ["{verifier_root}/check.py", "{workdir}", "{task_id}"],
        workdir=workdir,
        task_id=TASK_ID,
    )
    assert expanded == (f"{verifier}/check.py", str(workdir.resolve()), TASK_ID)

    result = bundle.run(workdir, task_id=TASK_ID)
    assert result.passed is False
    assert len(result.checks) == 2
    assert result.checks[0]["passed"] is True
    assert result.checks[1]["returncode"] == 3
    assert result.to_dict()["passed"] is False
    assert bundle.to_manifest_dict(tmp_path)["path"] == "verifier"


def test_task_spec_normalizes_manifest_and_derives_verifier_commands(tmp_path):
    spec = ExternalTaskSpec.from_manifest(_task(tmp_path, repository="file:///repo-a"), dataset_root=tmp_path)

    assert spec.is_local_fixture is True
    assert spec.repository_key == "file:///repo-a"
    assert spec.environment["max_steps"] == 5
    assert spec.environment["timeout_seconds"] == 30.0
    assert spec.manifest_sha256 == spec.manifest_sha256

    manifest = spec.to_manifest_dict()
    assert manifest["id"] == TASK_ID
    assert manifest["verifier"]["path"] == f"verifier/{TASK_ID}"
    assert spec.verifier_commands()[0][0] == sys.executable


def test_task_spec_adapts_to_code_task_without_materializing(tmp_path):
    spec = ExternalTaskSpec.from_manifest(_task(tmp_path), dataset_root=tmp_path)

    class _NoopMaterializer:
        def materialize(self, task, *, destination=None):  # pragma: no cover - must not run
            raise AssertionError("seed build must not run during adaptation")

    code_task = spec.to_code_task(_NoopMaterializer())
    assert code_task.name == TASK_ID
    assert code_task.resource_limits == {"timeout_seconds": 30.0, "max_steps": 5}
    assert code_task.checks[0].kind == "command"
    assert code_task.task_version == DATASET_VERSION
    assert code_task.source == f"external:{spec.repository_url}@{spec.base_commit}"


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda raw: raw.update(id=""), "id must be a non-empty string"),
        (lambda raw: raw.update(split="train"), "unsupported split"),
        (lambda raw: raw.update(repository="not-an-object"), "repository must be an object"),
        (lambda raw: raw["repository"].update(url="ftp://example/repo"), "repository.url must use http"),
        (lambda raw: raw["repository"].update(base_commit="deadbeef"), "base_commit must be a 40-char"),
        (lambda raw: raw["repository"].update(source_url="ssh://example/repo"), "source_url must use http"),
        (lambda raw: raw.update(difficulty="trivial"), "unsupported difficulty"),
        (lambda raw: raw.update(allowed_paths="src/**"), "allowed_paths must be a list"),
        (lambda raw: raw.update(allowed_paths=["src/\x00evil"]), "absolute or invalid path"),
        (lambda raw: raw.update(allowed_paths=["../escape"]), "escapes its workspace"),
        (lambda raw: raw.update(protected_paths=["src/**"]), "allowed_paths and protected_paths overlap"),
        (lambda raw: raw.update(environment="fast"), "environment must be an object"),
        (lambda raw: raw["environment"].update(install="pytest"), "environment.install must be a list of strings"),
        (lambda raw: raw["environment"].update(timeout_seconds="soon"), "environment limits are invalid"),
        (lambda raw: raw["environment"].update(max_steps=0), "environment limits are out of range"),
        (lambda raw: raw.update(reference="patch"), "reference must be an object"),
        (lambda raw: raw["reference"].update(patch_sha256="abc"), "patch_sha256 must be a lowercase SHA-256"),
        (lambda raw: raw["reference"].update(expected_changed_paths=[]), "expected_changed_paths cannot be empty"),
        (lambda raw: raw.update(metadata=["x"]), "metadata must be an object"),
    ],
)
def test_task_spec_rejects_invalid_manifest_fields(tmp_path, mutate, message):
    raw = _task(tmp_path)
    mutate(raw)
    with pytest.raises(ExternalTaskError, match=message):
        ExternalTaskSpec.from_manifest(raw, dataset_root=tmp_path)


def test_dataset_rejects_malformed_manifest_inputs(tmp_path):
    missing = tmp_path / "absent.json"
    with pytest.raises(ExternalTaskError, match="does not exist"):
        ExternalDataset.load(missing)

    broken = tmp_path / "broken.json"
    broken.write_text("{not json", encoding="utf-8")
    with pytest.raises(ExternalTaskError, match="cannot read dataset manifest"):
        ExternalDataset.load(broken)

    array = tmp_path / "array.json"
    array.write_text("[]", encoding="utf-8")
    with pytest.raises(ExternalTaskError, match="must be an object"):
        ExternalDataset.load(array)


def test_dataset_rejects_missing_version_and_empty_tasks(tmp_path):
    unversioned = tmp_path / "unversioned.json"
    unversioned.write_text(json.dumps({"tasks": [_task(tmp_path)]}), encoding="utf-8")
    with pytest.raises(ExternalTaskError, match="dataset_version must be a non-empty string"):
        ExternalDataset.load(unversioned)

    empty = tmp_path / "empty.json"
    empty.write_text(json.dumps({"dataset_version": DATASET_VERSION, "tasks": []}), encoding="utf-8")
    with pytest.raises(ExternalTaskError, match="tasks must be a non-empty list"):
        ExternalDataset.load(empty)


def test_dataset_rejects_version_drift_and_unknown_split(tmp_path):
    drifted = _task(tmp_path)
    path = _write_manifest(tmp_path, [drifted], version="external-test-2")
    with pytest.raises(ExternalTaskError, match="does not match the dataset manifest"):
        ExternalDataset.load(path)

    matching = _write_manifest(tmp_path, [_task(tmp_path)])
    with pytest.raises(ExternalTaskError, match="unsupported dataset split"):
        ExternalDataset.load(matching, split="train")


def test_dataset_rejects_duplicate_task_id(tmp_path):
    duplicate = [_task(tmp_path), _task(tmp_path)]
    path = _write_manifest(tmp_path, duplicate)
    with pytest.raises(ExternalTaskError, match="duplicate task id"):
        ExternalDataset.load(path)


def test_dataset_selects_split_and_exposes_manifest_view(tmp_path):
    validation = _task(tmp_path, "repo-a__issue-1", split="validation", repository="file:///repo-a")
    holdout = _task(tmp_path, "repo-b__issue-1", split="holdout", repository="file:///repo-b")
    path = _write_manifest(tmp_path, [validation, holdout])

    dataset = ExternalDataset.load(path)
    assert len(dataset.tasks) == 2
    assert [task.id for task in dataset.by_split("holdout")] == ["repo-b__issue-1"]
    with pytest.raises(ExternalTaskError, match="unsupported dataset split"):
        dataset.by_split("train")

    manifest = dataset.to_manifest_dict()
    assert manifest["schema_version"] == 1
    assert manifest["dataset_version"] == DATASET_VERSION

    selected = ExternalDataset.load(path, split="holdout")
    assert [task.id for task in selected.tasks] == ["repo-b__issue-1"]

    only_validation = tmp_path / "validation-only"
    only_validation.mkdir()
    sub = _task(only_validation, "repo-a__issue-1", repository="file:///repo-a")
    sub_path = _write_manifest(only_validation, [sub])
    with pytest.raises(ExternalTaskError, match="dataset split is empty"):
        ExternalDataset.load(sub_path, split="holdout")


def test_workspace_files_excludes_vcs_and_cache_paths(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("x", encoding="utf-8")
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "HEAD").write_text("ref", encoding="utf-8")
    (tmp_path / "__pycache__").mkdir()
    (tmp_path / "__pycache__" / "app.pyc").write_bytes(b"x")

    assert workspace_files(tmp_path) == {"src/app.py"}


def test_audit_reports_fixture_warnings_and_verifier_failures(tmp_path):
    dataset = ExternalDataset.load(_write_manifest(tmp_path, [_task(tmp_path, repository="file:///repo-a")]))

    audit = audit_external_dataset(dataset, check_repositories=True)
    assert audit["summary"]["valid_tasks"] == 1
    assert audit["summary"]["external_validity_evidence"] is False
    assert any("pipeline smoke only" in item for item in audit["warnings"])
    assert any("repository clone checks are not run by default" in item for item in audit["warnings"])

    contaminated = audit_external_dataset(dataset, agent_workspace=tmp_path)
    assert contaminated["summary"]["valid_tasks"] == 0
    assert contaminated["errors"]


def test_bootstrap_task_ci_handles_empty_input_deterministically():
    assert bootstrap_task_ci([]) == (0.0, 0.0)

    values = [0.0, 1.0, 0.5, 0.25]
    first = bootstrap_task_ci(values, seed=7)
    assert first == bootstrap_task_ci(values, seed=7)
    lower, upper = first
    assert 0.0 <= lower <= upper <= 1.0


def test_summarize_external_episodes_requires_task_and_scales_scope():
    with pytest.raises(ExternalTaskError, match="episode is missing task"):
        summarize_external_episodes([{"reward": 1}])

    episodes = [{"task": f"task-{index}", "reward": index % 2, "first_reward": 0} for index in range(12)]
    summary = summarize_external_episodes(episodes, seed=3)
    assert summary["tasks"] == 12
    assert summary["summary_scope"] == "task_bootstrap"
    assert summary["final_success"] == {"numerator": 6, "denominator": 12, "rate": 0.5}
    assert summary["task_level_first_rate"]["mean"] == 0.0
    assert len(summary["task_level_final_rate"]["bootstrap_95_ci"]) == 2
    assert summary["limitations"]
