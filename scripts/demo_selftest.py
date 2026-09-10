"""Run the deterministic AgentForge demo and print an auditable summary."""

from __future__ import annotations

import argparse
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agentforge.harness import run_selftest


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the no-LLM AgentForge demo")
    parser.add_argument("--out", default="runs/demo", help="trace output directory")
    args = parser.parse_args()

    started = time.perf_counter()
    with tempfile.TemporaryDirectory(prefix="agentforge-demo-") as directory:
        repo = Path(directory)
        (repo / "a.py").write_text("def foo():\n    # TODO\n    return 1\n", encoding="utf-8")
        (repo / "README.md").write_text("# sample\n", encoding="utf-8")
        result = run_selftest(str(repo), out_dir=args.out)

        output = repo / "out.txt"
        checks = {
            "output_exists": output.is_file(),
            "output_content": output.read_text(encoding="utf-8") == "agentforge selftest\n",
            "trace_exists": result.trace_path.is_file(),
        }
        changed = sorted(
            path.relative_to(repo).as_posix()
            for path in repo.rglob("*")
            if path.is_file() and path.name not in {"a.py", "README.md"}
        )

    elapsed = time.perf_counter() - started
    print("AgentForge demo")
    print(f"status: {'passed' if all(checks.values()) else 'failed'}")
    print(f"final_diff: created {', '.join(changed) or '(none)'}")
    print(f"checks: {checks}")
    print(f"steps: {len([step for step in result.trace.steps if step.get('kind') == 'action'])}")
    print("input_tokens: 0")
    print("output_tokens: 0")
    print(f"elapsed_seconds: {elapsed:.3f}")
    print(f"trace: {result.trace_path.resolve()}")
    return 0 if all(checks.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
