"""Declarative external-repository task specifications and audit helpers.

The internal benchmark uses Python callables to build seed repositories.  That
is convenient for unit tests, but it is not an auditable representation of an
external task.  This module keeps the external boundary data-only: manifests
are JSON, repository revisions are pinned, verifier bundles are hashed, and
the verifier is resolved outside the agent workspace.

The loader deliberately does not claim that a dataset is an external
benchmark merely because it is valid.  Local ``file://`` repositories are
accepted for pipeline smoke tests and are marked in the audit output.
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import platform
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Sequence
from urllib.parse import urlparse

EXTERNAL_SCHEMA_VERSION = 1
SUPPORTED_SPLITS = frozenset({"validation", "holdout"})
SHA256_RE = r"^[0-9a-f]{64}$"
COMMIT_RE = r"^[0-9a-f]{40}$"


class ExternalTaskError(ValueError):
    """Raised when an external task manifest cannot be trusted."""


def _canonical_json(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def hash_directory(root: str | Path) -> str:
    """Hash a verifier directory by normalized path and file bytes.

    Symlinks are rejected rather than followed.  A verifier bundle must be a
    stable, inspectable set of regular files.
    """

    base = Path(root).resolve()
    if not base.is_dir():
        raise ExternalTaskError(f"verifier bundle does not exist: {base}")
    entries: list[tuple[str, bytes]] = []
    for path in sorted(base.rglob("*")):
        if path.is_symlink():
            raise ExternalTaskError(f"verifier bundle contains a symlink: {path}")
        if path.is_dir():
            continue
        if not path.is_file():
            raise ExternalTaskError(f"verifier bundle contains a non-file: {path}")
        relative = path.relative_to(base).as_posix()
        entries.append((relative, path.read_bytes()))
    digest = hashlib.sha256()
    for relative, content in entries:
        digest.update(relative.encode("utf-8"))
        digest.update(b"\x00")
        digest.update(hashlib.sha256(content).digest())
        digest.update(b"\x00")
    return digest.hexdigest()


def _is_sha256(value: Any) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(c in "0123456789abcdef" for c in value)


def _is_commit(value: Any) -> bool:
    return isinstance(value, str) and len(value) == 40 and all(c in "0123456789abcdef" for c in value)


def _normal_path(value: str) -> str:
    return value.replace("\\", "/").strip("/")


def _validate_relative_pattern(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ExternalTaskError(f"{field_name} must contain non-empty strings")
    normalized = _normal_path(value)
    if normalized.startswith("/") or "\x00" in normalized:
        raise ExternalTaskError(f"{field_name} contains an absolute or invalid path: {value!r}")
    if any(part == ".." for part in normalized.split("/")):
        raise ExternalTaskError(f"{field_name} escapes its workspace: {value!r}")
    return normalized


def _path_matches(path: str, pattern: str) -> bool:
    path = _normal_path(path)
    pattern = _normal_path(pattern)
    if pattern.endswith("/**"):
        prefix = pattern[:-3].rstrip("/")
        return path == prefix or path.startswith(prefix + "/")
    return fnmatch.fnmatchcase(path, pattern)


def _patterns_overlap(left: str, right: str) -> bool:
    left = _normal_path(left)
    right = _normal_path(right)
    if left == right:
        return True
    for prefix, other in ((left[:-3].rstrip("/"), right), (right[:-3].rstrip("/"), left)):
        if left.endswith("/**") or right.endswith("/**"):
            if other == prefix or other.startswith(prefix + "/"):
                return True
    return False


def _require_string(data: dict[str, Any], key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ExternalTaskError(f"{key} must be a non-empty string")
    return value.strip()


def _require_list(data: dict[str, Any], key: str) -> list[Any]:
    value = data.get(key)
    if not isinstance(value, list):
        raise ExternalTaskError(f"{key} must be a list")
    return value


@dataclass(frozen=True)
class VerifierResult:
    passed: bool
    checks: tuple[dict[str, Any], ...]
    elapsed_seconds: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "checks": [dict(item) for item in self.checks],
            "elapsed_seconds": self.elapsed_seconds,
        }


@dataclass(frozen=True)
class VerifierBundle:
    """An immutable verifier reference resolved outside an agent workdir."""

    root: Path
    bundle_sha256: str
    commands: tuple[tuple[str, ...], ...]
    timeout_seconds: float

    @classmethod
    def from_manifest(
        cls,
        raw: dict[str, Any],
        *,
        dataset_root: str | Path,
        task_id: str,
    ) -> "VerifierBundle":
        if not isinstance(raw, dict):
            raise ExternalTaskError("verifier must be an object")
        expected = raw.get("bundle_sha256")
        if not _is_sha256(expected):
            raise ExternalTaskError(f"verifier.bundle_sha256 must be a lowercase SHA-256: {task_id}")
        expected_hash = str(expected)
        raw_commands = raw.get("commands")
        if not isinstance(raw_commands, list) or not raw_commands:
            raise ExternalTaskError(f"verifier.commands must be a non-empty list: {task_id}")
        commands: list[tuple[str, ...]] = []
        for index, command in enumerate(raw_commands):
            if not isinstance(command, list) or not command or not all(
                isinstance(part, str) and part for part in command
            ):
                raise ExternalTaskError(f"verifier.commands[{index}] must be a non-empty argv list")
            commands.append(tuple(command))
        timeout = raw.get("timeout_seconds", 120)
        try:
            timeout_value = float(timeout)
        except (TypeError, ValueError) as exc:
            raise ExternalTaskError("verifier.timeout_seconds must be numeric") from exc
        if timeout_value <= 0 or timeout_value > 3600:
            raise ExternalTaskError("verifier.timeout_seconds must be between 0 and 3600")
        relative_root = raw.get("path") or f"verifier/{task_id}"
        if not isinstance(relative_root, str):
            raise ExternalTaskError("verifier.path must be a relative string")
        root = Path(dataset_root).resolve() / relative_root
        try:
            root.relative_to(Path(dataset_root).resolve())
        except ValueError as exc:
            raise ExternalTaskError("verifier.path escapes the dataset root") from exc
        return cls(root, expected_hash, tuple(commands), timeout_value)

    def validate(self, *, agent_workspace: str | Path | None = None) -> list[str]:
        warnings: list[str] = []
        if not self.root.is_dir():
            raise ExternalTaskError(f"verifier bundle is missing: {self.root}")
        actual = hash_directory(self.root)
        if actual != self.bundle_sha256:
            raise ExternalTaskError(
                f"verifier hash mismatch for {self.root}: expected {self.bundle_sha256}, got {actual}"
            )
        if agent_workspace is not None:
            workspace = Path(agent_workspace).resolve()
            try:
                self.root.relative_to(workspace)
            except ValueError:
                pass
            else:
                raise ExternalTaskError("verifier bundle must not be inside the agent workspace")
        return warnings

    def expand_command(self, command: Sequence[str], *, workdir: str | Path | None = None, task_id: str = "") -> tuple[str, ...]:
        replacements = {
            "{verifier_root}": str(self.root),
            "{workdir}": str(Path(workdir).resolve()) if workdir is not None else "{workdir}",
            "{task_id}": task_id,
        }
        expanded: list[str] = []
        for value in command:
            item = value
            for token, replacement in replacements.items():
                item = item.replace(token, replacement)
            expanded.append(item)
        return tuple(expanded)

    def run(self, workdir: str | Path, *, task_id: str = "") -> VerifierResult:
        """Run every immutable command with ``shell=False`` semantics."""

        self.validate(agent_workspace=workdir)
        from .eval import run_check_command

        started = time.perf_counter()
        checks: list[dict[str, Any]] = []
        passed = True
        for command in self.commands:
            expanded = self.expand_command(command, workdir=workdir, task_id=task_id)
            result = run_check_command(expanded, str(workdir), timeout=self.timeout_seconds)
            check = {
                "command": list(expanded),
                "returncode": result.returncode,
                "stdout": result.stdout[-4000:],
                "stderr": result.stderr[-4000:],
                "timed_out": result.timed_out,
                "output_limited": result.output_limited,
                "error": result.error,
                "passed": result.returncode == 0 and not result.error and not result.timed_out,
            }
            checks.append(check)
            passed = passed and bool(check["passed"])
        return VerifierResult(passed, tuple(checks), time.perf_counter() - started)

    def to_manifest_dict(self, dataset_root: str | Path) -> dict[str, Any]:
        root = self.root.resolve()
        relative = root.relative_to(Path(dataset_root).resolve()).as_posix()
        return {
            "path": relative,
            "bundle_sha256": self.bundle_sha256,
            "commands": [list(command) for command in self.commands],
            "timeout_seconds": self.timeout_seconds,
        }


@dataclass(frozen=True)
class ExternalTaskSpec:
    id: str
    dataset_version: str
    split: str
    repository_url: str
    license: str
    base_commit: str
    source_url: str
    instruction: str
    category: str
    difficulty: str
    allowed_paths: tuple[str, ...]
    protected_paths: tuple[str, ...]
    environment: dict[str, Any]
    verifier: VerifierBundle
    reference: dict[str, Any]
    manifest_root: Path
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_manifest(cls, raw: dict[str, Any], *, dataset_root: str | Path) -> "ExternalTaskSpec":
        if not isinstance(raw, dict):
            raise ExternalTaskError("each external task must be an object")
        task_id = _require_string(raw, "id")
        dataset_version = _require_string(raw, "dataset_version")
        split = _require_string(raw, "split").lower()
        if split not in SUPPORTED_SPLITS:
            raise ExternalTaskError(f"unsupported split for {task_id}: {split}")
        repository = raw.get("repository")
        if not isinstance(repository, dict):
            raise ExternalTaskError(f"repository must be an object: {task_id}")
        repository_url = _require_string(repository, "url")
        if urlparse(repository_url).scheme not in {"http", "https", "file"}:
            raise ExternalTaskError(f"repository.url must use http(s) or file://: {task_id}")
        license_name = _require_string(repository, "license")
        base_commit = _require_string(repository, "base_commit").lower()
        if not _is_commit(base_commit):
            raise ExternalTaskError(f"repository.base_commit must be a 40-char lowercase SHA: {task_id}")
        source_url = _require_string(repository, "source_url")
        if urlparse(source_url).scheme not in {"http", "https", "file"}:
            raise ExternalTaskError(f"repository.source_url must use http(s) or file://: {task_id}")
        instruction = _require_string(raw, "instruction")
        category = _require_string(raw, "category").lower()
        difficulty = _require_string(raw, "difficulty").lower()
        if difficulty not in {"easy", "medium", "hard"}:
            raise ExternalTaskError(f"unsupported difficulty for {task_id}: {difficulty}")
        allowed = tuple(_validate_relative_pattern(item, "allowed_paths") for item in _require_list(raw, "allowed_paths"))
        protected = tuple(
            _validate_relative_pattern(item, "protected_paths") for item in _require_list(raw, "protected_paths")
        )
        for left in allowed:
            for right in protected:
                if _patterns_overlap(left, right):
                    raise ExternalTaskError(f"allowed_paths and protected_paths overlap for {task_id}: {left}, {right}")
        environment = raw.get("environment")
        if not isinstance(environment, dict):
            raise ExternalTaskError(f"environment must be an object: {task_id}")
        python_version = _require_string(environment, "python")
        installs = environment.get("install", [])
        if not isinstance(installs, list) or not all(isinstance(item, str) and item for item in installs):
            raise ExternalTaskError(f"environment.install must be a list of strings: {task_id}")
        try:
            timeout = float(environment.get("timeout_seconds", 120))
            max_steps = int(environment.get("max_steps", 20))
        except (TypeError, ValueError) as exc:
            raise ExternalTaskError(f"environment limits are invalid: {task_id}") from exc
        if timeout <= 0 or timeout > 3600 or max_steps < 1:
            raise ExternalTaskError(f"environment limits are out of range: {task_id}")
        normalized_environment = dict(environment)
        normalized_environment["python"] = python_version
        normalized_environment["install"] = installs
        normalized_environment["timeout_seconds"] = timeout
        normalized_environment["max_steps"] = max_steps
        reference = raw.get("reference")
        if not isinstance(reference, dict):
            raise ExternalTaskError(f"reference must be an object: {task_id}")
        patch_hash = reference.get("patch_sha256")
        if not _is_sha256(patch_hash):
            raise ExternalTaskError(f"reference.patch_sha256 must be a lowercase SHA-256: {task_id}")
        expected_paths = tuple(
            _validate_relative_pattern(item, "reference.expected_changed_paths")
            for item in _require_list(reference, "expected_changed_paths")
        )
        if not expected_paths:
            raise ExternalTaskError(f"reference.expected_changed_paths cannot be empty: {task_id}")
        normalized_reference = dict(reference)
        normalized_reference["expected_changed_paths"] = list(expected_paths)
        normalized_reference["patch_sha256"] = patch_hash
        root = Path(dataset_root).resolve()
        verifier = VerifierBundle.from_manifest(raw.get("verifier", {}), dataset_root=root, task_id=task_id)
        metadata = raw.get("metadata", {})
        if not isinstance(metadata, dict):
            raise ExternalTaskError(f"metadata must be an object: {task_id}")
        return cls(
            id=task_id,
            dataset_version=dataset_version,
            split=split,
            repository_url=repository_url,
            license=license_name,
            base_commit=base_commit,
            source_url=source_url,
            instruction=instruction,
            category=category,
            difficulty=difficulty,
            allowed_paths=allowed,
            protected_paths=protected,
            environment=normalized_environment,
            verifier=verifier,
            reference=normalized_reference,
            manifest_root=root,
            metadata=dict(metadata),
        )

    @property
    def is_local_fixture(self) -> bool:
        return urlparse(self.repository_url).scheme == "file"

    @property
    def repository_key(self) -> str:
        return self.repository_url.rstrip("/").lower()

    def to_manifest_dict(self) -> dict[str, Any]:
        repository = {
            "url": self.repository_url,
            "license": self.license,
            "base_commit": self.base_commit,
            "source_url": self.source_url,
        }
        return {
            "id": self.id,
            "dataset_version": self.dataset_version,
            "split": self.split,
            "repository": repository,
            "instruction": self.instruction,
            "category": self.category,
            "difficulty": self.difficulty,
            "allowed_paths": list(self.allowed_paths),
            "protected_paths": list(self.protected_paths),
            "environment": dict(self.environment),
            "verifier": self.verifier.to_manifest_dict(self.manifest_root),
            "reference": dict(self.reference),
            "metadata": dict(self.metadata),
        }

    @property
    def manifest_sha256(self) -> str:
        return sha256_bytes(_canonical_json(self.to_manifest_dict()))

    def changed_paths_allowed(self, paths: Iterable[str]) -> tuple[bool, list[str]]:
        violations: list[str] = []
        for raw_path in paths:
            path = _normal_path(str(raw_path))
            if any(_path_matches(path, pattern) for pattern in self.protected_paths):
                violations.append(path)
                continue
            if not any(_path_matches(path, pattern) for pattern in self.allowed_paths):
                violations.append(path)
        return not violations, sorted(set(violations))

    def verifier_commands(self, *, workdir: str | Path | None = None) -> tuple[tuple[str, ...], ...]:
        return tuple(
            self.verifier.expand_command(command, workdir=workdir, task_id=self.id)
            for command in self.verifier.commands
        )

    def to_code_task(self, materializer: "RepositoryMaterializer"):
        """Adapt the immutable verifier to the existing evaluator.

        The verifier remains outside the agent workdir.  Path-policy checks are
        performed by the external benchmark runner after the episode and are
        intentionally not reduced to a reference-tree hash.
        """

        from .eval import Check, CodeTask

        def build_seed(workdir: str) -> None:
            materializer.materialize(self, destination=workdir)

        checks = [
            Check(kind="command", command=command, timeout=self.verifier.timeout_seconds)
            for command in self.verifier_commands()
        ]
        return CodeTask(
            name=self.id,
            instruction=self.instruction,
            build_seed=build_seed,
            checks=checks,
            difficulty=self.difficulty,
            source=f"external:{self.repository_url}@{self.base_commit}",
            task_version=self.dataset_version,
            resource_limits={
                "timeout_seconds": self.environment["timeout_seconds"],
                "max_steps": self.environment["max_steps"],
            },
        )


@dataclass(frozen=True)
class ExternalDataset:
    root: Path
    version: str
    tasks: tuple[ExternalTaskSpec, ...]
    manifest_sha256: str
    dataset_sha256: str
    manifest_path: Path

    @classmethod
    def load(cls, path: str | Path, *, split: str | None = None) -> "ExternalDataset":
        source = Path(path).resolve()
        manifest_path = source / "manifest.json" if source.is_dir() else source
        if not manifest_path.is_file():
            raise ExternalTaskError(f"dataset manifest does not exist: {manifest_path}")
        try:
            raw = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ExternalTaskError(f"cannot read dataset manifest: {manifest_path}") from exc
        if not isinstance(raw, dict):
            raise ExternalTaskError("dataset manifest must be an object")
        version = _require_string(raw, "dataset_version")
        raw_tasks = raw.get("tasks")
        if not isinstance(raw_tasks, list) or not raw_tasks:
            raise ExternalTaskError("dataset manifest.tasks must be a non-empty list")
        root = manifest_path.parent
        tasks = tuple(ExternalTaskSpec.from_manifest(item, dataset_root=root) for item in raw_tasks)
        if any(task.dataset_version != version for task in tasks):
            raise ExternalTaskError("task dataset_version does not match the dataset manifest")
        dataset = cls(
            root=root,
            version=version,
            tasks=tasks,
            manifest_sha256=sha256_bytes(_canonical_json(raw)),
            dataset_sha256="",
            manifest_path=manifest_path,
        )
        dataset.validate()
        selected = tuple(task for task in tasks if split is None or task.split == split)
        if split is not None and split not in SUPPORTED_SPLITS:
            raise ExternalTaskError(f"unsupported dataset split: {split}")
        if not selected:
            raise ExternalTaskError(f"dataset split is empty: {split}")
        return cls(
            root=root,
            version=version,
            tasks=selected,
            manifest_sha256=dataset.manifest_sha256,
            dataset_sha256=sha256_bytes(
                _canonical_json(
                    {
                        "manifest_sha256": dataset.manifest_sha256,
                        "verifier_sha256": sorted(task.verifier.bundle_sha256 for task in tasks),
                    }
                )
            ),
            manifest_path=manifest_path,
        )

    def validate(self) -> None:
        ids: set[str] = set()
        repositories: dict[str, str] = {}
        for task in self.tasks:
            if task.id in ids:
                raise ExternalTaskError(f"duplicate task id: {task.id}")
            ids.add(task.id)
            previous = repositories.get(task.repository_key)
            if previous is not None and previous != task.split:
                raise ExternalTaskError(
                    f"repository crosses validation/holdout split: {task.repository_url} ({previous}, {task.split})"
                )
            repositories[task.repository_key] = task.split
            task.verifier.validate()

    def by_split(self, split: str) -> tuple[ExternalTaskSpec, ...]:
        if split not in SUPPORTED_SPLITS:
            raise ExternalTaskError(f"unsupported dataset split: {split}")
        return tuple(task for task in self.tasks if task.split == split)

    def to_manifest_dict(self) -> dict[str, Any]:
        return {"schema_version": EXTERNAL_SCHEMA_VERSION, "dataset_version": self.version, "tasks": [task.to_manifest_dict() for task in self.tasks]}


@dataclass
class RepositoryMaterializer:
    """Clone pinned revisions into fresh workspaces with an optional cache."""

    cache_root: Path = Path("runs/external-benchmark/cache")
    git_timeout_seconds: float = 300.0

    def materialize(self, task: ExternalTaskSpec, *, destination: str | Path | None = None) -> Path:
        cache_root = self.cache_root.resolve()
        cache_root.mkdir(parents=True, exist_ok=True)
        key = sha256_bytes(f"{task.repository_url}\0{task.base_commit}".encode("utf-8"))
        cached = cache_root / key
        if not cached.is_dir():
            staging = Path(tempfile.mkdtemp(prefix=f"{key}-", dir=cache_root))
            try:
                self._clone(task.repository_url, staging)
                self._git_checkout(staging, task.base_commit)
                staging.replace(cached)
            except Exception:
                shutil.rmtree(staging, ignore_errors=True)
                raise
        target = Path(destination).resolve() if destination is not None else Path(
            tempfile.mkdtemp(prefix=f"agentforge-external-{task.id}-")
        ).resolve()
        if target.exists():
            if any(target.iterdir()):
                raise ExternalTaskError(f"materialization destination is not empty: {target}")
            target.rmdir()
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(cached, target, symlinks=False)
        return target

    def _clone(self, url: str, destination: Path) -> None:
        command = ["git", "clone", "--no-hardlinks", "--no-checkout", url, str(destination)]
        result = subprocess.run(
            command,
            cwd=str(destination.parent),
            capture_output=True,
            text=True,
            timeout=self.git_timeout_seconds,
            check=False,
        )
        if result.returncode != 0:
            raise ExternalTaskError(f"git clone failed for {url}: {result.stderr[-1000:]}")

    def _git_checkout(self, repository: Path, commit: str) -> None:
        result = subprocess.run(
            ["git", "checkout", "--detach", commit],
            cwd=str(repository),
            capture_output=True,
            text=True,
            timeout=self.git_timeout_seconds,
            check=False,
        )
        if result.returncode != 0:
            raise ExternalTaskError(f"git checkout failed for {commit}: {result.stderr[-1000:]}")
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(repository),
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        if head.returncode != 0 or head.stdout.strip().lower() != commit:
            raise ExternalTaskError(f"materialized repository is not at base commit {commit}")


def workspace_files(root: str | Path) -> set[str]:
    """Return regular workspace paths, excluding VCS and runtime metadata."""

    base = Path(root).resolve()
    result: set[str] = set()
    for path in base.rglob("*"):
        if path.is_symlink() or not path.is_file():
            continue
        relative = path.relative_to(base).as_posix()
        if any(part in {".git", ".venv", "__pycache__", ".pytest_cache"} for part in Path(relative).parts):
            continue
        result.add(relative)
    return result


def audit_external_dataset(
    dataset: ExternalDataset,
    *,
    agent_workspace: str | Path | None = None,
    check_repositories: bool = False,
) -> dict[str, Any]:
    """Return a machine-readable audit without silently repairing manifests."""

    errors: list[str] = []
    warnings: list[str] = []
    tasks: list[dict[str, Any]] = []
    for task in dataset.tasks:
        item: dict[str, Any] = {
            "id": task.id,
            "split": task.split,
            "repository": task.repository_url,
            "base_commit": task.base_commit,
            "verifier_sha256": task.verifier.bundle_sha256,
            "local_fixture": task.is_local_fixture,
            "base_checked": False,
            "reference_checked": False,
            "errors": [],
        }
        if task.is_local_fixture:
            warnings.append(f"{task.id}: file:// repository is pipeline smoke only, not external validity evidence")
        try:
            task.verifier.validate(agent_workspace=agent_workspace)
        except ExternalTaskError as exc:
            item["errors"].append(str(exc))
        if check_repositories:
            warnings.append(f"{task.id}: repository clone checks are not run by default in this audit")
        if item["errors"]:
            errors.extend(f"{task.id}: {message}" for message in item["errors"])
        tasks.append(item)
    return {
        "schema_version": EXTERNAL_SCHEMA_VERSION,
        "audit_version": "external-audit-v1",
        "dataset_version": dataset.version,
        "dataset_sha256": dataset.dataset_sha256,
        "manifest_sha256": dataset.manifest_sha256,
        "platform": platform.platform(),
        "tasks": tasks,
        "summary": {
            "tasks": len(tasks),
            "valid_tasks": sum(not item["errors"] for item in tasks),
            "errors": len(errors),
            "warnings": len(warnings),
            "base_fail_reference_pass": "not_checked",
            "external_validity_evidence": False,
        },
        "errors": errors,
        "warnings": warnings,
    }


def _git_changed_paths(workdir: str | Path) -> list[str]:
    result = subprocess.run(
        ["git", "status", "--porcelain", "--untracked-files=all"],
        cwd=str(workdir),
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if result.returncode != 0:
        return []
    paths: list[str] = []
    for line in result.stdout.splitlines():
        if len(line) < 4:
            continue
        path = line[3:]
        if " -> " in path:
            path = path.split(" -> ", 1)[1]
        paths.append(_normal_path(path.strip('"')))
    return sorted(set(paths))


def _current_git_commit() -> str | None:
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=str(root),
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    return result.stdout.strip() if result.returncode == 0 else None


def run_external_episode(
    cfg: Any,
    task: ExternalTaskSpec,
    *,
    materializer: RepositoryMaterializer,
    out_dir: str | Path,
    verify_enabled: bool = False,
    max_attempts: int = 1,
    agent: Any = None,
    model_name: str | None = None,
    config_id: str = "custom",
    config_metadata: dict[str, Any] | None = None,
    trial: int = 0,
    seed: int = 0,
    dataset_sha256: str | None = None,
) -> dict[str, Any]:
    """Run one external episode while preserving verifier/path metadata.

    This is intentionally a thin adapter around the existing evaluator.  The
    report is marked ``pipeline_smoke`` for local repositories and callers
    must not interpret it as the formal external benchmark.
    """

    from .eval import solve_task

    workdir = materializer.materialize(task)
    baseline = workspace_files(workdir)
    started = time.perf_counter()
    code_task = task.to_code_task(materializer)
    try:
        current_agent = agent(workdir) if callable(agent) else agent
        result = solve_task(
            cfg,
            code_task,
            out_dir=out_dir,
            keep_workdir=True,
            verify_enabled=verify_enabled,
            max_attempts=max_attempts,
            agent=current_agent,
            workdir=str(workdir),
        )
        changed = sorted(workspace_files(workdir) - baseline)
        changed.extend(path for path in _git_changed_paths(workdir) if path not in changed)
        allowed, violations = task.changed_paths_allowed(changed)
        verifier_result = task.verifier.run(workdir, task_id=task.id)
        reward = int(result.reward and allowed and verifier_result.passed)
        episode = {
            "task": task.id,
            "dataset_version": task.dataset_version,
            "dataset_sha256": dataset_sha256 or task.manifest_sha256,
            "split": task.split,
            "model": model_name or str(getattr(cfg, "model", "")),
            "config": config_id,
            "config_metadata": dict(config_metadata or {}),
            "trial": trial,
            "seed": seed,
            "git_commit": _current_git_commit(),
            "python": sys.version,
            "platform": platform.platform(),
            "category": task.category,
            "repository": task.repository_url,
            "base_commit": task.base_commit,
            "verifier_sha256": task.verifier.bundle_sha256,
            "first_reward": int(result.attempts[0]["reward"]) if result.attempts else 0,
            "reward": reward,
            "changed_paths": changed,
            "path_policy_passed": allowed,
            "disallowed_paths": violations,
            "verifier": verifier_result.to_dict(),
            "trace_path": str(result.trace_path) if result.trace_path else None,
            "diff": result.diff,
            "attempts": result.attempts,
            "elapsed_seconds": time.perf_counter() - started,
            "input_tokens": result.input_tokens,
            "output_tokens": result.output_tokens,
            "pipeline_scope": "pipeline_smoke" if task.is_local_fixture else "external_candidate",
        }
        return episode
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def bootstrap_task_ci(values: Sequence[float], *, samples: int = 2000, seed: int = 0) -> tuple[float, float]:
    """Deterministic percentile bootstrap over task-level values."""

    import random

    if not values:
        return (0.0, 0.0)
    rng = random.Random(seed)
    task_values = [float(value) for value in values]
    means: list[float] = []
    for _ in range(max(100, samples)):
        draw = [task_values[rng.randrange(len(task_values))] for _ in task_values]
        means.append(sum(draw) / len(draw))
    means.sort()
    lower = means[int(0.025 * (len(means) - 1))]
    upper = means[int(0.975 * (len(means) - 1))]
    return lower, upper


def summarize_external_episodes(episodes: Sequence[dict[str, Any]], *, seed: int = 0) -> dict[str, Any]:
    """Aggregate episodes by task and report task-level bootstrap intervals."""

    by_task: dict[str, list[dict[str, Any]]] = {}
    for episode in episodes:
        task_id = str(episode.get("task", ""))
        if not task_id:
            raise ExternalTaskError("episode is missing task")
        by_task.setdefault(task_id, []).append(episode)
    task_rates = [sum(int(item.get("reward", 0)) for item in rows) / len(rows) for rows in by_task.values()]
    first_rates = [
        sum(int(item.get("first_reward", item.get("reward", 0))) for item in rows) / len(rows)
        for rows in by_task.values()
    ]
    final_success = sum(int(item.get("reward", 0)) for item in episodes)
    first_success = sum(int(item.get("first_reward", item.get("reward", 0))) for item in episodes)
    return {
        "schema_version": EXTERNAL_SCHEMA_VERSION,
        "summary_scope": "descriptive_only" if len(by_task) < 12 else "task_bootstrap",
        "tasks": len(by_task),
        "episodes": len(episodes),
        "first_success": {"numerator": first_success, "denominator": len(episodes), "rate": first_success / len(episodes) if episodes else 0.0},
        "final_success": {"numerator": final_success, "denominator": len(episodes), "rate": final_success / len(episodes) if episodes else 0.0},
        "task_level_final_rate": {
            "mean": sum(task_rates) / len(task_rates) if task_rates else 0.0,
            "bootstrap_95_ci": bootstrap_task_ci(task_rates, seed=seed),
        },
        "task_level_first_rate": {
            "mean": sum(first_rates) / len(first_rates) if first_rates else 0.0,
            "bootstrap_95_ci": bootstrap_task_ci(first_rates, seed=seed),
        },
        "limitations": [
            "Local file:// repositories are pipeline smoke only.",
            "A formal external result requires frozen holdout data, two models, and the planned trial count.",
            "Bootstrap resamples tasks rather than treating trials as independent tasks.",
        ],
    }
