from __future__ import annotations

import dataclasses
import json

import pytest

from agentforge.fault_injection import (
    FAULT_FORMAL_DECLARED_TOTAL,
    FAULT_FORMAL_MATRIX_TOTAL,
    FAULT_SCENARIOS,
    FAULT_SUITE_VERSION,
    SCENARIO_BY_NAME,
    FaultScenario,
    FaultTrial,
    SideEffectLedger,
    _tree_hash,
    fault_matrix_plan,
    fault_matrix_sha256,
    summarize_fault_trials,
)
from scripts.run_fault_matrix import _validate_existing, run_matrix
from scripts.summarize_fault_matrix import build_summary


def _result(scenario: FaultScenario, **overrides) -> dict:
    result = {
        "resume_attempted": True,
        "resume_succeeded": scenario.expected == "recover",
        "verification_passed": True,
        "duplicate_side_effects": 0,
        "orphan_processes": 0,
        "trace_complete": True,
        "state_consistent": True,
        "recovery_latency_seconds": 0.02,
        "false_success": False,
    }
    result.update(overrides)
    return result


def _trial(name: str, *, trial: int = 0, observed: bool = True, **overrides) -> FaultTrial:
    scenario = SCENARIO_BY_NAME[name]
    return FaultTrial.create(
        scenario,
        trial=trial,
        seed=0,
        injection={"trigger_event": scenario.trigger_event, "action": scenario.action, "observed": observed},
        result=_result(scenario, **overrides),
        artifacts={},
    )


def test_fault_scenario_registry_is_unique_and_declares_expected_outcomes():
    names = [scenario.name for scenario in FAULT_SCENARIOS]
    assert len(names) == len(set(names)) == 14
    assert set(SCENARIO_BY_NAME) == set(names)

    fail_closed = SCENARIO_BY_NAME["F05_effect_before_completion"]
    assert fail_closed.expected == "fail-closed"
    assert fail_closed.recoverable is False
    assert SCENARIO_BY_NAME["F01_llm_before_request"].expected == "recover"


def test_fault_matrix_scopes_are_fixed_and_hashed():
    smoke = fault_matrix_plan("smoke")
    pilot = fault_matrix_plan("pilot")
    formal = fault_matrix_plan("formal")
    assert len(smoke) == 14
    assert len(pilot) == 70
    assert len(formal) == FAULT_FORMAL_MATRIX_TOTAL == 460
    assert FAULT_FORMAL_DECLARED_TOTAL == 410
    assert fault_matrix_sha256(formal) == fault_matrix_sha256(fault_matrix_plan("formal"))
    with pytest.raises(ValueError, match="smoke, pilot, or formal"):
        fault_matrix_plan("custom")


def test_fault_trial_round_trip_defaults_checkpoint_sequence():
    trial = _trial("F01_llm_before_request")
    assert trial.result["resume_checkpoint_sequence"] is None
    assert trial.suite_version == FAULT_SUITE_VERSION

    restored = FaultTrial.from_dict(json.loads(json.dumps(trial.to_dict())))
    assert restored.scenario == trial.scenario
    assert restored.expected == "recover"
    assert restored.trial == 0


def test_existing_fault_trial_validation_includes_injection_identity(tmp_path):
    scenario = SCENARIO_BY_NAME["F01_llm_before_request"]
    trial = _trial(scenario.name)
    path = tmp_path / "trial.json"
    path.write_text(json.dumps(trial.to_dict()), encoding="utf-8")
    expected = {
        "scenario": scenario.name,
        "trial": trial.trial,
        "seed": trial.seed,
        "expected": trial.expected,
        "trigger_event": scenario.trigger_event,
        "action": scenario.action,
    }

    loaded, error = _validate_existing(path, expected)
    assert loaded is not None
    assert error is None

    tampered = dict(expected)
    tampered["action"] = "unexpected-action"
    loaded, error = _validate_existing(path, tampered)
    assert loaded is None
    assert error == "identity mismatch: action"


def test_existing_fault_trial_allows_legacy_empty_side_effect_ledger(tmp_path):
    scenario = SCENARIO_BY_NAME["F01_llm_before_request"]
    trial = _trial(scenario.name)
    trial.artifacts["side_effect_ledger"] = "side_effects.jsonl"
    path = tmp_path / "trial.json"
    path.write_text(json.dumps(trial.to_dict()), encoding="utf-8")
    expected = {
        "scenario": scenario.name,
        "trial": trial.trial,
        "seed": trial.seed,
        "expected": trial.expected,
        "trigger_event": scenario.trigger_event,
        "action": scenario.action,
    }

    loaded, error = _validate_existing(path, expected)
    assert loaded is not None
    assert error is None


def test_fault_summary_preserves_protocol_block(tmp_path):
    scenario = SCENARIO_BY_NAME["F01_llm_before_request"]
    trial = _trial(scenario.name)
    (tmp_path / "run-manifest.json").write_text(
        json.dumps(
            {
                "plan": [{"scenario": scenario.name, "trial": 0, "seed": 0}],
                "matrix_sha256": "matrix",
                "scope": "formal",
                "protocol_consistent": False,
                "protocol_warnings": ["protocol totals disagree"],
            }
        ),
        encoding="utf-8",
    )

    summary = build_summary(tmp_path, [trial])
    assert summary["protocol_consistent"] is False
    assert summary["protocol_warnings"] == ["protocol totals disagree"]
    assert summary["complete"] is False


def test_fault_formal_protocol_mismatch_blocks_worker_execution(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "scripts.run_fault_matrix.run_fault_trial",
        lambda *args, **kwargs: pytest.fail("formal protocol mismatch must not start a trial"),
    )

    summary = run_matrix(scope="formal", out_dir=tmp_path, seed=0, timeout_seconds=0.1)
    assert summary["planned_trials"] == FAULT_FORMAL_MATRIX_TOTAL == 460
    assert summary["missing_trials"] == 460
    assert summary["protocol_consistent"] is False
    assert summary["complete"] is False
    assert not list(tmp_path.rglob("trial.json"))


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"schema_version": 99}, "unsupported fault trial schema version"),
        ({"suite_version": "other"}, "unsupported fault suite version"),
        ({"scenario": "F99_unknown"}, "unknown fault scenario"),
        ({"trial": -1}, "must be non-negative"),
        ({"seed": -1}, "must be non-negative"),
        ({"injection": {"trigger_event": "llm.request.started"}}, "injection is missing"),
        ({"result": {}}, "result is missing"),
        ({"artifacts": []}, "artifacts must be an object"),
    ],
)
def test_fault_trial_validate_rejects_tampered_records(changes, message):
    tampered = dataclasses.replace(_trial("F01_llm_before_request"), **changes)
    with pytest.raises(ValueError, match=message):
        tampered.validate()


def test_fault_trial_from_dict_rejects_non_mapping():
    with pytest.raises(ValueError, match="must be an object"):
        FaultTrial.from_dict([])


def test_side_effect_ledger_separates_invocation_from_effect(tmp_path):
    ledger = SideEffectLedger(tmp_path / "ledger.jsonl")
    assert ledger.records() == []

    ledger.append("send-email", {"to": "a@example.invalid"})
    ledger.append("send-email", {"to": "a@example.invalid"}, effect_count=0)

    records = ledger.records()
    assert [record["semantic_key"] for record in records] == ["send-email", "send-email"]
    assert [record["invocation_count"] for record in records] == [1, 1]
    assert [record["effect_count"] for record in records] == [1, 0]


def test_side_effect_ledger_skips_corrupt_lines(tmp_path):
    path = tmp_path / "ledger.jsonl"
    path.write_text('{"semantic_key": "a"}\n\nnot json\n[1, 2]\n', encoding="utf-8")
    assert SideEffectLedger(path).records() == [{"semantic_key": "a"}]


def test_tree_hash_ignores_caches_and_handles_missing_root(tmp_path):
    absent = _tree_hash(tmp_path / "missing")
    assert isinstance(absent, str) and len(absent) == 64

    root = tmp_path / "root"
    (root / "src").mkdir(parents=True)
    (root / "src" / "a.py").write_text("x", encoding="utf-8")
    baseline = _tree_hash(root)

    (root / "__pycache__").mkdir()
    (root / "__pycache__" / "a.pyc").write_bytes(b"x")
    assert _tree_hash(root) == baseline

    (root / "src" / "a.py").write_text("y", encoding="utf-8")
    assert _tree_hash(root) != baseline


def test_summarize_classifies_scope_by_trial_count():
    smoke = [_trial(name, trial=index) for index, name in enumerate(SCENARIO_BY_NAME)]
    assert summarize_fault_trials(smoke)["scope"] == "smoke"

    pilot = [_trial(name, trial=trial) for trial in range(5) for name in SCENARIO_BY_NAME]
    assert summarize_fault_trials(pilot)["scope"] == "pilot"

    assert summarize_fault_trials([_trial("F01_llm_before_request")])["scope"] == "custom"


def test_summarize_keeps_fail_closed_out_of_recover_metrics():
    trials = [
        _trial("F01_llm_before_request"),
        _trial("F05_effect_before_completion", resume_succeeded=False),
    ]
    summary = summarize_fault_trials(trials)

    assert summary["metrics"]["resume_success_rate"] == {"numerator": 1, "denominator": 1, "rate": 1.0}
    assert summary["metrics"]["fail_closed_correctness_rate"] == {"numerator": 1, "denominator": 1, "rate": 1.0}
    assert summary["scenarios"]["F05_effect_before_completion"]["expected"] == "fail-closed"
    assert summary["thresholds"]["fail_closed_correctness_rate"] == 1.0
    assert summary["limitations"]


def test_summarize_reports_none_rate_for_unobserved_trials():
    summary = summarize_fault_trials([_trial("F01_llm_before_request", observed=False)])

    assert summary["metrics"]["resume_success_rate"] == {"numerator": 0, "denominator": 0, "rate": None}
    assert summary["valid_trials"] == 0
    assert summary["invalid_trials"] == 1
    assert summary["recovery_latency_seconds"] == {"p50": None, "p95": None}
    assert summary["multi_platform_evidence"] is False


def test_summarize_reports_latency_percentiles_and_platform_spread():
    platforms = ["linux", "windows", "linux", "windows"]
    trials = [
        dataclasses.replace(
            _trial("F01_llm_before_request", trial=index, recovery_latency_seconds=index / 100),
            platform=platform,
        )
        for index, platform in enumerate(platforms)
    ]
    summary = summarize_fault_trials(trials)

    assert summary["platforms"] == ["linux", "windows"]
    assert summary["multi_platform_evidence"] is True
    assert summary["recovery_latency_seconds"]["p50"] == pytest.approx(0.015)
    assert summary["recovery_latency_seconds"]["p95"] == pytest.approx(0.02)
    assert summary["metrics"]["state_consistency_rate"]["denominator"] == 4
