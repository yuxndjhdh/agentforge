"""Audit a declarative external-task dataset and write JSON evidence."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agentforge.external_tasks import (
    ExternalDataset,
    ExternalTaskError,
    atomic_write_json,
    audit_external_dataset,
)


def _render_markdown(report: dict) -> str:
    summary = report["summary"]
    lines = [
        "# External Task Audit",
        "",
        f"Dataset: `{report['dataset_version']}`; tasks: `{summary['tasks']}`; valid: `{summary['valid_tasks']}`.",
        "",
        f"Base fail/reference pass: `{summary['base_fail_reference_pass']}`.",
        f"External validity evidence: `{summary['external_validity_evidence']}`.",
        "",
        "| Task | Split | Base checked | Base passed | Reference checked | Reference passed | Errors |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for item in report["tasks"]:
        lines.append(
            "| {id} | {split} | {base_checked} | {base_passed} | {reference_checked} | "
            "{reference_passed} | {errors} |".format(
                id=item["id"],
                split=item["split"],
                base_checked=item["base_checked"],
                base_passed=item["base_passed"],
                reference_checked=item["reference_checked"],
                reference_passed=item["reference_passed"],
                errors="; ".join(item["errors"]) or "-",
            )
        )
    if report["warnings"]:
        lines.extend(["", "## Warnings", ""])
        lines.extend(f"- {warning}" for warning in report["warnings"])
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Audit external task manifests and verifier bundles")
    parser.add_argument("--dataset", required=True, help="manifest.json or dataset directory")
    parser.add_argument("--out", default="runs/external-benchmark/audit/audit.json", help="audit JSON path")
    parser.add_argument(
        "--base-reference",
        default="runs/external-benchmark/audit/base_reference.json",
        help="base/reference evidence JSON path",
    )
    parser.add_argument("--report", default="docs/external_task_audit.md", help="Markdown report path")
    parser.add_argument("--agent-workspace", default=None, help="workspace used to assert verifier isolation")
    parser.add_argument(
        "--check-repositories",
        action="store_true",
        help="clone pinned revisions and run base/reference verifier checks",
    )
    parser.add_argument(
        "--require-base-reference",
        action="store_true",
        help="fail the audit when base-fail/reference-pass evidence is unavailable",
    )
    args = parser.parse_args(argv)
    try:
        dataset = ExternalDataset.load(args.dataset)
        if args.check_repositories:
            dataset.validate_lock()
        report = audit_external_dataset(
            dataset,
            agent_workspace=args.agent_workspace,
            check_repositories=args.check_repositories,
            execute_repository_checks=args.check_repositories,
            require_base_reference=args.require_base_reference or args.check_repositories,
        )
    except ExternalTaskError as exc:
        parser.error(str(exc))
    rendered = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True)
    print(rendered)
    if args.out:
        atomic_write_json(args.out, report)
    base_reference = {
        "schema_version": 1,
        "dataset_version": report["dataset_version"],
        "dataset_sha256": report["dataset_sha256"],
        "base_reference": [
            {
                "task": item["id"],
                "base_passed": item["base_passed"],
                "reference_passed": item["reference_passed"],
                "base_checked": item["base_checked"],
                "reference_checked": item["reference_checked"],
                "reference_changed_paths": item["reference_changed_paths"],
                "errors": item["errors"],
            }
            for item in report["tasks"]
        ],
    }
    if args.base_reference:
        atomic_write_json(args.base_reference, base_reference)
    if args.report:
        report_path = Path(args.report)
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(_render_markdown(report), encoding="utf-8")
    return 0 if not report["errors"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
