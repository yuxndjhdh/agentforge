# Fault Injection Report

Scope: `pilot`; trials: `70`; valid: `70`.

This report is generated from `trial.json` files. Smoke and pilot results are not the formal 410-trial matrix.

| Metric | Numerator | Denominator | Rate |
| --- | ---: | ---: | ---: |
| resume_success_rate | 45 | 45 | 1.000 |
| duplicate_side_effect_rate | 0 | 10 | 0.000 |
| cancellation_reclaim_rate | 5 | 5 | 1.000 |
| timeout_reclaim_rate | 5 | 5 | 1.000 |
| trace_completeness | 70 | 70 | 1.000 |
| state_consistency_rate | 70 | 70 | 1.000 |
| orphan_run_rate | 0 | 70 | 0.000 |
| false_success_rate | 0 | 70 | 0.000 |
| fail_closed_correctness_rate | 15 | 15 | 1.000 |

## Scenario Coverage

| Scenario | Trials | Observed | Expected |
| --- | ---: | ---: | --- |
| F01_llm_before_request | 5 | 5 | recover |
| F02_llm_after_response | 5 | 5 | recover |
| F03_tool_before_start | 5 | 5 | recover |
| F04_tool_completed_persisted | 5 | 5 | recover |
| F05_effect_before_completion | 5 | 5 | fail-closed |
| F06_checkpoint_completed | 5 | 5 | recover |
| F07_jsonl_partial_tail | 5 | 5 | recover |
| F08_checkpoint_corruption | 5 | 5 | recover |
| F09_checkpoint_schema | 5 | 5 | fail-closed |
| F10_sqlite_temporarily_unwritable | 5 | 5 | recover |
| F11_api_process_restart | 5 | 5 | recover |
| F12_docker_unavailable | 5 | 5 | fail-closed |
| F13_run_timeout | 5 | 5 | timeout |
| F14_user_cancel | 5 | 5 | cancelled |

## Limitations

- Smoke and pilot results are not the formal 410-trial matrix.
- F05 demonstrates the local fail-closed boundary; it cannot prove exactly-once behavior for an arbitrary external service.
- Docker unavailability is represented by an explicit deterministic fail-closed probe; no daemon is disrupted by this runner.
