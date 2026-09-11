from __future__ import annotations

import json

import pytest

from scripts.summarize_benchmark import build_report


def _report(variant: str) -> dict:
    return {
        "experiment_id": variant,
        "experiment": {
            "experiment_id": variant,
            "context_compression_enabled": variant in {"B", "D"},
            "verify_enabled": variant in {"C", "D"},
            "max_attempts": 3 if variant in {"C", "D"} else 1,
        },
        "model": "fake",
        "git": {"commit": "abc123", "dirty": False},
        "task_summary": {"count": 1},
        "task_order": ["task"],
        "summary": {
            "tasks": 1,
            "episodes": 1,
            "num_trials": 1,
            "pass@1": 1.0 if variant != "A" else 0.0,
            "pass@3": None,
            "pass@5": None,
            "first_success_rate": 0.0,
            "final_success_rate": 1.0 if variant != "A" else 0.0,
            "avg_steps": 2.0,
            "avg_input_tokens": 20.0,
            "avg_output_tokens": 10.0,
            "estimated_cost": 0.001,
            "p50_latency_seconds": 0.1,
            "p95_latency_seconds": 0.2,
            "compression_trigger_rate": 1.0 if variant in {"B", "D"} else 0.0,
            "verify_incremental_cost": 0.0001 if variant in {"C", "D"} else 0.0,
        },
        "episodes": [
            {
                "reward": 1,
                "trace_path": "trace.json",
                "attempts": [],
                "elapsed_seconds": 0.1,
                "steps": 2,
            }
        ],
    }


def test_build_report_is_generated_from_all_variants(tmp_path):
    paths = {}
    for variant in ("A", "B", "C", "D"):
        path = tmp_path / f"{variant}.json"
        path.write_text(json.dumps(_report(variant)), encoding="utf-8")
        paths[variant] = path

    report = build_report(paths)
    assert "# Code Agent Benchmark Report" in report
    assert "| A |" in report
    assert "| A -> D |" in report
    assert "abc123" in report
    assert "Do not edit metrics manually" in report


def test_build_report_marks_unconfigured_cost_unavailable(tmp_path):
    paths = {}
    for variant in ("A", "B", "C", "D"):
        path = tmp_path / f"{variant}.json"
        path.write_text(json.dumps(_report(variant)), encoding="utf-8")
        paths[variant] = path

    report = build_report(paths)

    assert "Cost |" in report
    assert "unavailable" in report


def test_build_report_rejects_wrong_variant(tmp_path):
    paths = {}
    for variant in ("A", "B", "C", "D"):
        path = tmp_path / f"{variant}.json"
        path.write_text(json.dumps(_report("A")), encoding="utf-8")
        paths[variant] = path
    with pytest.raises(ValueError, match="expected experiment_id"):
        build_report(paths)
