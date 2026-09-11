"""Audit a declarative external-task dataset and write JSON evidence."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agentforge.external_tasks import ExternalDataset, ExternalTaskError, audit_external_dataset


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Audit external task manifests and verifier bundles")
    parser.add_argument("--dataset", required=True, help="manifest.json or dataset directory")
    parser.add_argument("--out", default="", help="optional audit JSON path")
    parser.add_argument("--agent-workspace", default=None, help="workspace used to assert verifier isolation")
    parser.add_argument(
        "--check-repositories",
        action="store_true",
        help="record repository-check limitations; cloning is intentionally not implicit",
    )
    args = parser.parse_args(argv)
    try:
        dataset = ExternalDataset.load(args.dataset)
        report = audit_external_dataset(
            dataset,
            agent_workspace=args.agent_workspace,
            check_repositories=args.check_repositories,
        )
    except ExternalTaskError as exc:
        parser.error(str(exc))
    rendered = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True)
    print(rendered)
    if args.out:
        path = Path(args.out)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(rendered + "\n", encoding="utf-8")
    return 0 if not report["errors"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
