"""Run the P1-A three-task local pipeline smoke.

The repositories are generated locally and are deliberately reported as
``pipeline_smoke``. This exercises materialization, verifier isolation,
base-fail/reference-pass checks, fake-agent execution, trace, and summary
plumbing without making an external-validity claim.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agentforge.external_tasks import (
    ExternalDataset,
    RepositoryMaterializer,
    audit_external_dataset,
    hash_directory,
    run_external_episode,
    sha256_bytes,
    summarize_external_episodes,
)


class _ActionStep:
    step_number = 1
    tool_calls: list = []
    observations = "local pipeline smoke"
    token_usage = None


class _Memory:
    def __init__(self) -> None:
        self.steps = []


class _Model:
    compressions: list = []
    context_compression_enabled = False


class _SmokeAgent:
    def __init__(self, workdir: str, expected: str) -> None:
        self.workdir = Path(workdir)
        self.expected = expected
        self.memory = _Memory()
        self.model = _Model()

    def run(self, task: str, reset: bool = True, **kwargs) -> str:
        del task, reset, kwargs
        (self.workdir / "src" / "value.txt").write_text(self.expected + "\n", encoding="utf-8")
        self.memory.steps.append(_ActionStep())
        return "local smoke change applied"


def _git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True
    )
    return result.stdout.strip()


def _commit_repository(root: Path, index: int) -> tuple[str, str, str, str]:
    root.mkdir(parents=True)
    _git(root, "init")
    _git(root, "config", "user.email", "smoke@example.invalid")
    _git(root, "config", "user.name", "AgentForge Smoke")
    (root / "src").mkdir()
    (root / "src" / "value.txt").write_text(f"base-{index}\n", encoding="utf-8")
    _git(root, "add", "src/value.txt")
    _git(root, "commit", "-m", "base")
    base = _git(root, "rev-parse", "HEAD")
    expected = f"fixed-{index}"
    (root / "src" / "value.txt").write_text(expected + "\n", encoding="utf-8")
    _git(root, "add", "src/value.txt")
    _git(root, "commit", "-m", "reference fix")
    fixed = _git(root, "rev-parse", "HEAD")
    patch = subprocess.run(
        ["git", "diff", base, fixed], cwd=str(root), capture_output=True, check=True
    ).stdout
    return base, fixed, expected, sha256_bytes(patch)


def run_smoke(out_dir: str | Path) -> dict:
    target = Path(out_dir).resolve()
    target.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="agentforge-p1a-") as temporary:
        root = Path(temporary)
        dataset_root = root / "dataset"
        verifier_root = dataset_root / "verifier"
        verifier_root.mkdir(parents=True)
        tasks = []
        reference_checks = []
        for index in range(1, 4):
            repository = root / f"repo-{index}"
            base, fixed, expected, patch_hash = _commit_repository(repository, index)
            task_id = f"local-repo-{index}__smoke"
            task_verifier = verifier_root / task_id
            task_verifier.mkdir()
            (task_verifier / "check.py").write_text(
                "from pathlib import Path\n"
                f"expected = {expected!r}\n"
                "actual = Path.cwd().joinpath('src', 'value.txt').read_text(encoding='utf-8').strip()\n"
                "raise SystemExit(0 if actual == expected else 1)\n",
                encoding="utf-8",
            )
            check_command = [sys.executable, str(task_verifier / "check.py")]
            tasks.append(
                {
                    "id": task_id,
                    "dataset_version": "external-v1-pipeline-smoke",
                    "split": "validation",
                    "repository": {
                        "url": repository.as_uri(),
                        "license": "MIT",
                        "base_commit": base,
                        "source_url": f"{repository.as_uri()}#issue-smoke-{index}",
                    },
                    "instruction": f"Update src/value.txt to the requested behavior for smoke task {index}.",
                    "category": "bug-fix",
                    "difficulty": "easy",
                    "allowed_paths": ["src/**"],
                    "protected_paths": ["external-verifier/**", ".git/**"],
                    "environment": {"python": "3.12", "install": [], "timeout_seconds": 30, "max_steps": 5},
                    "verifier": {
                        "path": f"verifier/{task_id}",
                        "bundle_sha256": hash_directory(task_verifier),
                        "commands": [check_command],
                    },
                    "reference": {
                        "patch_sha256": patch_hash,
                        "expected_changed_paths": ["src/value.txt"],
                    },
                    "metadata": {"expected_value": expected},
                }
            )
            reference_checks.append({"task": task_id, "source": str(repository), "fixed_commit": fixed})
        manifest_path = dataset_root / "manifest.json"
        manifest_path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "dataset_version": "external-v1-pipeline-smoke",
                    "tasks": tasks,
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        dataset = ExternalDataset.load(manifest_path)
        audit = audit_external_dataset(dataset)
        materializer = RepositoryMaterializer(target / "cache")
        base_reference = []
        episodes = []
        for task, reference in zip(dataset.tasks, reference_checks, strict=True):
            base_workspace = materializer.materialize(task)
            base_result = task.verifier.run(base_workspace, task_id=task.id)
            reference_workspace = Path(tempfile.mkdtemp(prefix="agentforge-reference-"))
            try:
                subprocess.run(["git", "clone", "--no-hardlinks", reference["source"], str(reference_workspace)], check=True, capture_output=True)
                _git(reference_workspace, "checkout", "--detach", reference["fixed_commit"])
                reference_result = task.verifier.run(reference_workspace, task_id=task.id)
            finally:
                import shutil

                shutil.rmtree(base_workspace, ignore_errors=True)
                shutil.rmtree(reference_workspace, ignore_errors=True)
            base_reference.append(
                {
                    "task": task.id,
                    "base_passed": base_result.passed,
                    "reference_passed": reference_result.passed,
                }
            )
        for task in dataset.tasks:
            episodes.append(
                run_external_episode(
                    type("Cfg", (), {"model": "pipeline-smoke"})(),
                    task,
                    materializer=materializer,
                    out_dir=target / "traces",
                    agent=lambda workdir, expected=task.metadata["expected_value"]: _SmokeAgent(workdir, expected),
                )
            )
        summary = summarize_external_episodes(episodes, seed=0)
        report = {
            "scope": "pipeline_smoke",
            "external_validity_evidence": False,
            "dataset_version": dataset.version,
            "dataset_sha256": dataset.dataset_sha256,
            "manifest_sha256": dataset.manifest_sha256,
            "audit": audit,
            "base_reference": base_reference,
            "episodes": episodes,
            "summary": summary,
            "limitations": [
                "All three repositories and the agent edits are local deterministic fixtures.",
                "This validates pipeline plumbing only; it is not an external benchmark success rate.",
            ],
        }
    (target / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the P1-A three-task pipeline smoke")
    parser.add_argument("--out", default="runs/external-benchmark/p1a-smoke")
    args = parser.parse_args(argv)
    report = run_smoke(args.out)
    print(json.dumps({"scope": report["scope"], "tasks": len(report["episodes"]), "summary": report["summary"]}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
