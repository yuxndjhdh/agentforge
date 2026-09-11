"""Run the deterministic fault-injection smoke or pilot matrix."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agentforge.fault_injection import FAULT_SCENARIOS, run_fault_matrix


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run AgentForge fault-injection trials")
    parser.add_argument("--out", default="runs/fault-injection/v1")
    parser.add_argument("--trials", type=int, default=1, help="trials per scenario; 1 is smoke, 5 is pilot")
    parser.add_argument("--scenario", action="append", choices=[item.name for item in FAULT_SCENARIOS])
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--timeout", type=float, default=20.0)
    args = parser.parse_args(argv)
    selected = [item for item in FAULT_SCENARIOS if not args.scenario or item.name in set(args.scenario)]
    records, summary = run_fault_matrix(
        scenarios=selected,
        trials_per_scenario=args.trials,
        out_dir=args.out,
        seed=args.seed,
        timeout_seconds=args.timeout,
    )
    print(json.dumps({"trials": len(records), "summary": summary}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
