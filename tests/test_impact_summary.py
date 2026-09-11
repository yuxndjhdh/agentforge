from __future__ import annotations

import json

from scripts.summarize_impact import build_impact_summary, write_summary


def _report(tmp_path, variant: str) -> dict:
    trace = tmp_path / f"{variant}.trace.json"
    trace.write_text("{}\n", encoding="utf-8")
    first_reward = 0 if variant in {"C", "D"} else 1
    final_reward = 1
    return {
        "schema_version": 2,
        "experiment_id": variant,
        "experiment": {"experiment_id": variant},
        "model": "fake",
        "git_commit": "abc123",
        "workspace_dirty": False,
        "task_order": ["task"],
        "config": {"input_cost_per_million": 0, "output_cost_per_million": 0},
        "summary": {"pass@1": 1.0, "pass@k": 1.0, "p50_latency_seconds": 1.0, "p95_latency_seconds": 2.0},
        "episodes": [
            {
                "first_reward": first_reward,
                "reward": final_reward,
                "trace_path": str(trace),
                "attempts": [{"reward": first_reward}],
                "elapsed_seconds": 1.0,
                "steps": 2,
                "input_tokens": 100 if variant == "C" else 75,
                "output_tokens": 20 if variant == "C" else 15,
                "first_input_tokens": 50,
                "first_output_tokens": 10,
                "attempt_count": 2 if variant in {"C", "D"} else 1,
                "compression_trigger_count": 1 if variant == "D" else 0,
                "compression_saved_tokens": 10 if variant == "D" else 0,
            }
        ],
    }


def test_impact_summary_derives_comparisons_and_source_provenance(tmp_path):
    paths = {}
    for variant in ("A", "B", "C", "D"):
        path = tmp_path / f"{variant}.json"
        path.write_text(json.dumps(_report(tmp_path, variant)), encoding="utf-8")
        paths[variant] = path

    summary = build_impact_summary(paths, repo_root=tmp_path)

    assert summary["source"]["episodes_total"] == 4
    assert summary["variants"]["C"]["metrics"]["recovered_first_attempt_failures"] == 1
    assert summary["variants"]["C"]["metrics"]["failure_recovery_rate"] == 1.0
    assert summary["comparisons"]["context_compression"]["avg_input_token_reduction_rate"] == 0.25
    assert summary["variants"]["D"]["metrics"]["cost"]["status"] == "unavailable"
    assert len(summary["source"]["reports"]["A"]["sha256"]) == 64


def test_impact_summary_writes_atomically(tmp_path):
    paths = {}
    for variant in ("A", "B", "C", "D"):
        path = tmp_path / f"{variant}.json"
        path.write_text(json.dumps(_report(tmp_path, variant)), encoding="utf-8")
        paths[variant] = path
    output = write_summary(paths, tmp_path / "impact.json", repo_root=tmp_path)
    assert output.exists()
    assert json.loads(output.read_text(encoding="utf-8"))["schema_version"] == 1
