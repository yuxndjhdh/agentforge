"""Rebuild a fault-injection summary from immutable trial.json files."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agentforge.external_tasks import atomic_write_json
from agentforge.fault_injection import FAULT_FORMAL_DECLARED_TOTAL, FaultTrial, summarize_fault_trials


def load_trials(root: str | Path) -> list[FaultTrial]:
    base = Path(root)
    records: list[FaultTrial] = []
    for path in sorted(base.rglob("trial.json")):
        records.append(FaultTrial.from_dict(json.loads(path.read_text(encoding="utf-8"))))
    if not records:
        raise ValueError(f"no trial.json files found under {base}")
    return records


def validate_against_manifest(root: str | Path, trials: list[FaultTrial]) -> dict:
    manifest_path = Path(root) / "run-manifest.json"
    if not manifest_path.is_file():
        return {
            "present": False,
            "missing": [],
            "duplicates": [],
            "matrix_sha256": None,
            "planned": len(trials),
            "protocol_consistent": True,
            "protocol_warnings": [],
            "protocol_version": None,
            "formal_trial_total": FAULT_FORMAL_DECLARED_TOTAL,
        }
    raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or not isinstance(raw.get("plan"), list):
        raise ValueError(f"invalid fault run manifest: {manifest_path}")
    protocol_consistent = raw.get("protocol_consistent", True)
    protocol_warnings = raw.get("protocol_warnings", [])
    protocol_version = raw.get("protocol_version", raw.get("suite_version", "unknown"))
    formal_trial_total = raw.get("formal_trial_total", FAULT_FORMAL_DECLARED_TOTAL)
    if (
        not isinstance(protocol_consistent, bool)
        or not isinstance(protocol_warnings, list)
        or not isinstance(protocol_version, str)
        or not isinstance(formal_trial_total, int)
    ):
        raise ValueError(f"invalid protocol metadata in fault run manifest: {manifest_path}")
    expected = [
        (str(item.get("scenario")), int(item.get("trial", -1)), int(item.get("seed", -1)))
        for item in raw["plan"]
    ]
    actual = [(trial.scenario, trial.trial, trial.seed) for trial in trials]
    expected_set = set(expected)
    actual_set = set(actual)
    duplicates = sorted({key for key in actual if actual.count(key) > 1})
    missing = sorted(expected_set - actual_set)
    unexpected = sorted(actual_set - expected_set)
    if unexpected:
        duplicates.extend(unexpected)
    return {
        "present": True,
        "missing": missing,
        "duplicates": duplicates,
        "matrix_sha256": raw.get("matrix_sha256"),
        "planned": len(expected),
        "scope": raw.get("scope"),
        "protocol_consistent": protocol_consistent,
        "protocol_warnings": protocol_warnings,
        "protocol_version": protocol_version,
        "formal_trial_total": formal_trial_total,
    }


def render_markdown(summary: dict) -> str:
    lines = [
        "# Fault Injection Report",
        "",
        f"Scope: `{summary['scope']}`; trials: `{summary['trials']}`; valid: `{summary['valid_trials']}`; complete: `{summary.get('complete', False)}`.",
        f"Protocol: `{summary.get('protocol_version', 'unknown')}`; formal trials: `{summary.get('formal_trial_total', FAULT_FORMAL_DECLARED_TOTAL)}`; consistent: `{summary.get('protocol_consistent', True)}`.",
        f"Platforms: `{', '.join(summary.get('platforms', [])) or 'unknown'}`; multi-platform evidence: `{summary.get('multi_platform_evidence', False)}`.",
        "",
        f"This report is generated from `trial.json` files. Smoke and pilot results are not the formal {summary.get('formal_trial_total', FAULT_FORMAL_DECLARED_TOTAL)}-trial matrix.",
        "",
        "| Metric | Numerator | Denominator | Rate |",
        "| --- | ---: | ---: | ---: |",
    ]
    for name, value in summary["metrics"].items():
        rate = "n/a" if value["rate"] is None else f"{value['rate']:.3f}"
        lines.append(f"| {name} | {value['numerator']} | {value['denominator']} | {rate} |")
    lines.extend(["", "## Scenario Coverage", "", "| Scenario | Trials | Observed | Expected |", "| --- | ---: | ---: | --- |"])
    for name, value in summary["scenarios"].items():
        lines.append(f"| {name} | {value['trials']} | {value['observed']} | {value['expected']} |")
    lines.extend(["", "## Limitations", ""])
    lines.extend(f"- {item}" for item in summary["limitations"])
    lines.extend(f"- Protocol: {item}" for item in summary.get("protocol_warnings", []))
    if summary.get("missing_trials") or summary.get("duplicate_trials"):
        lines.extend(["", "## Integrity", ""])
        lines.append(f"- Missing trials: `{summary.get('missing_trials', 0)}`")
        lines.append(f"- Duplicate/unexpected trials: `{summary.get('duplicate_trials', 0)}`")
    return "\n".join(lines) + "\n"


def build_summary(root: str | Path, trials: list[FaultTrial]) -> dict:
    manifest_check = validate_against_manifest(root, trials)
    summary = summarize_fault_trials(trials)
    if manifest_check["present"]:
        summary["scope"] = manifest_check["scope"] or summary["scope"]
        summary["matrix_sha256"] = manifest_check["matrix_sha256"]
        summary["planned_trials"] = manifest_check["planned"]
        summary["missing_trials"] = len(manifest_check["missing"])
        summary["duplicate_trials"] = len(manifest_check["duplicates"])
        summary["protocol_consistent"] = manifest_check["protocol_consistent"]
        summary["protocol_warnings"] = manifest_check["protocol_warnings"]
        summary["protocol_version"] = manifest_check["protocol_version"]
        summary["formal_trial_total"] = manifest_check["formal_trial_total"]
        summary["complete"] = (
            not manifest_check["missing"]
            and not manifest_check["duplicates"]
            and summary["invalid_trials"] == 0
            and manifest_check["protocol_consistent"]
        )
        summary["manifest_missing"] = manifest_check["missing"]
        summary["manifest_duplicates"] = manifest_check["duplicates"]
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Summarize fault-injection trials")
    parser.add_argument("--runs", required=True, help="fault-injection output root")
    parser.add_argument("--out", default="", help="summary JSON path")
    parser.add_argument("--report", default="", help="Markdown report path")
    args = parser.parse_args(argv)
    trials = load_trials(args.runs)
    summary = build_summary(args.runs, trials)
    rendered = json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    print(rendered)
    if args.out:
        atomic_write_json(args.out, summary)
    if args.report:
        path = Path(args.report)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.tmp")
        temporary.write_text(render_markdown(summary), encoding="utf-8")
        temporary.replace(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
