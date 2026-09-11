"""Freeze a declarative external-task manifest without changing its contents.

This command copies only the JSON manifest and emits split/license indexes.
Repository clones and episode workspaces belong under ``runs/`` and are not
checked into the repository.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agentforge.external_tasks import ExternalDataset, ExternalTaskError, sha256_bytes


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def prepare_dataset(source: str | Path, destination: str | Path) -> dict[str, object]:
    dataset = ExternalDataset.load(source)
    destination_path = Path(destination).resolve()
    destination_path.mkdir(parents=True, exist_ok=True)
    manifest = dataset.to_manifest_dict()
    for task in dataset.tasks:
        relative = task.verifier.root.resolve().relative_to(dataset.root.resolve())
        target = destination_path / relative
        if target.exists():
            shutil.rmtree(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(task.verifier.root, target, symlinks=False)
    _write_json(destination_path / "manifest.json", manifest)
    for split in ("validation", "holdout"):
        _write_json(
            destination_path / f"{split}.json",
            {
                "schema_version": 1,
                "dataset_version": dataset.version,
                "split": split,
                "tasks": [task.to_manifest_dict() for task in dataset.tasks if task.split == split],
            },
        )
    _write_json(
        destination_path / "licenses.json",
        {
            "dataset_version": dataset.version,
            "licenses": {
                task.repository_url: task.license
                for task in dataset.tasks
            },
        },
    )
    manifest_hash = sha256_bytes(json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8"))
    (destination_path / "manifest.sha256").write_text(manifest_hash + "\n", encoding="ascii")
    return {
        "dataset_version": dataset.version,
        "tasks": len(dataset.tasks),
        "validation": len(dataset.by_split("validation")),
        "holdout": len(dataset.by_split("holdout")),
        "manifest_sha256": manifest_hash,
        "destination": str(destination_path),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Freeze and index an external task manifest")
    parser.add_argument("--manifest", required=True, help="source manifest.json or dataset directory")
    parser.add_argument("--out", required=True, help="frozen dataset directory")
    args = parser.parse_args(argv)
    try:
        print(json.dumps(prepare_dataset(args.manifest, args.out), ensure_ascii=False, indent=2))
    except ExternalTaskError as exc:
        parser.error(str(exc))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
