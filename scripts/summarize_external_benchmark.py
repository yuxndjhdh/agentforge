"""Summarize real external episodes without over-claiming their scope."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agentforge.external_tasks import ExternalTaskError, summarize_external_episodes


def load_episodes(path: str | Path) -> list[dict]:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    if isinstance(raw, dict):
        raw = raw.get("episodes")
    if not isinstance(raw, list) or not all(isinstance(item, dict) for item in raw):
        raise ExternalTaskError("episode input must be a JSON list or an object with episodes")
    return raw


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Summarize external benchmark episodes")
    parser.add_argument("--episodes", required=True, help="episodes.json or benchmark report")
    parser.add_argument("--out", default="", help="optional summary JSON path")
    parser.add_argument("--seed", type=int, default=0)
    args = parser.parse_args(argv)
    try:
        summary = summarize_external_episodes(load_episodes(args.episodes), seed=args.seed)
    except (OSError, json.JSONDecodeError, ExternalTaskError) as exc:
        parser.error(str(exc))
    rendered = json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True)
    print(rendered)
    if args.out:
        path = Path(args.out)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(rendered + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
