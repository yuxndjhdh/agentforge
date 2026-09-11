"""Rebuild a fault-injection summary from immutable trial.json files."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agentforge.fault_injection import FaultTrial, summarize_fault_trials


def load_trials(root: str | Path) -> list[FaultTrial]:
    base = Path(root)
    records: list[FaultTrial] = []
    for path in sorted(base.rglob("trial.json")):
        records.append(FaultTrial.from_dict(json.loads(path.read_text(encoding="utf-8"))))
    if not records:
        raise ValueError(f"no trial.json files found under {base}")
    return records


def render_markdown(summary: dict) -> str:
    lines = [
        "# Fault Injection Report",
        "",
        f"Scope: `{summary['scope']}`; trials: `{summary['trials']}`; valid: `{summary['valid_trials']}`.",
        "",
        "This report is generated from `trial.json` files. Smoke and pilot results are not the formal 410-trial matrix.",
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
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Summarize fault-injection trials")
    parser.add_argument("--runs", required=True, help="fault-injection output root")
    parser.add_argument("--out", default="", help="summary JSON path")
    parser.add_argument("--report", default="", help="Markdown report path")
    args = parser.parse_args(argv)
    summary = summarize_fault_trials(load_trials(args.runs))
    rendered = json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    print(rendered)
    if args.out:
        path = Path(args.out)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(rendered, encoding="utf-8")
    if args.report:
        path = Path(args.report)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(render_markdown(summary), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
