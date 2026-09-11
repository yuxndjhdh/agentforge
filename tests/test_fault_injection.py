from __future__ import annotations

import json

from agentforge.fault_injection import (
    FAULT_SCENARIOS,
    FaultTrial,
    _pid_exists,
    run_fault_trial,
    summarize_fault_trials,
)


def test_fault_trial_schema_round_trip():
    scenario = FAULT_SCENARIOS[0]
    trial = FaultTrial.create(
        scenario,
        trial=2,
        seed=9,
        injection={"trigger_event": scenario.trigger_event, "action": scenario.action, "observed": True},
        result={
            "resume_attempted": True,
            "resume_succeeded": True,
            "verification_passed": True,
            "duplicate_side_effects": 0,
            "orphan_processes": 0,
            "trace_complete": True,
            "state_consistent": True,
            "recovery_latency_seconds": 0.01,
            "false_success": False,
        },
        artifacts={"trace": "trace.json"},
    )
    loaded = FaultTrial.from_dict(json.loads(json.dumps(trial.to_dict())))
    assert loaded.scenario == scenario.name
    assert loaded.trial == 2


def test_fault_controller_runs_independent_tool_replay_smoke(tmp_path):
    trial = run_fault_trial("F04_tool_completed_persisted", out_dir=tmp_path, timeout_seconds=10)
    assert trial.injection["observed"] is True
    assert trial.result["resume_succeeded"] is True
    assert trial.result["duplicate_side_effects"] == 0
    assert (tmp_path / "F04_tool_completed_persisted" / "0" / "trial.json").is_file()


def test_fault_controller_reclaims_known_child_tree_after_worker_exit(tmp_path):
    trial = run_fault_trial("F14_user_cancel", out_dir=tmp_path, timeout_seconds=10)
    pids = json.loads((tmp_path / "F14_user_cancel" / "0" / "pids.json").read_text())
    assert trial.result["orphan_processes"] == 0
    assert not _pid_exists(pids["child"])
    assert not _pid_exists(pids["grandchild"])


def test_fault_summary_keeps_fail_closed_out_of_resume_denominator():
    trials = []
    for scenario in FAULT_SCENARIOS[:2]:
        trials.append(
            FaultTrial.create(
                scenario,
                trial=0,
                seed=0,
                injection={"trigger_event": scenario.trigger_event, "action": scenario.action, "observed": True},
                result={
                    "resume_attempted": True,
                    "resume_succeeded": True,
                    "verification_passed": True,
                    "duplicate_side_effects": 0,
                    "orphan_processes": 0,
                    "trace_complete": True,
                    "state_consistent": True,
                    "recovery_latency_seconds": 0.01,
                    "false_success": False,
                },
                artifacts={},
            )
        )
    summary = summarize_fault_trials(trials)
    assert summary["metrics"]["resume_success_rate"]["denominator"] == 2
    assert summary["metrics"]["fail_closed_correctness_rate"]["denominator"] == 0
