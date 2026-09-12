# Fault Injection Protocol

Protocol version: `p2-formal-460-v2`; suite version: `fault-v2`.

P2 uses an independent controller and worker process. The controller creates a
trial directory, starts `python -m agentforge.fault_injection --worker`, waits
for a persisted lifecycle barrier, injects the selected action, optionally
restarts the worker, and writes an immutable `trial.json`. The worker uses the
existing AgentForge runtime, SQLite store, JSONL events, checkpoints, trace
writer, and a separate append-only side-effect ledger.

## Scenarios

The registry contains the 14 planned scenarios: LLM and tool termination
windows, checkpoint completion, JSONL tail corruption, checkpoint corruption
and schema mismatch, temporary SQLite failure, process restart, explicit
container fail-closed probing, timeout, and cancel. Each trial records the
trigger event and `injection.observed`; an unobserved trigger is invalid and is
never counted as a passing recovery.

## Outcome semantics

`recover` scenarios require a successful resume plus a complete trace and
consistent state. `fail-closed` scenarios (including an external effect whose
completion record is missing, incompatible checkpoint schema, and unavailable
container runtime) require explicit refusal rather than guessed replay.

The side-effect ledger distinguishes invocation/effect count. F04 should replay
the persisted observation without a second effect. F05 demonstrates the
boundary where local state cannot prove exactly-once for an arbitrary external
service; automatic replay is rejected.

## Scope

`scripts/run_fault_matrix.py --trials 1` is the P2-A smoke path. `--trials 5`
is the P2-B pilot path (70 trials across all scenarios). The formal matrix is
The formal matrix contains 460 trials and is not claimed by this repository
change. Its per-scenario counts are:

| Scenario | Formal trials |
| --- | ---: |
| F01-F03 | 20 each |
| F04-F06 | 50 each |
| F07-F10 | 20 each |
| F11 | 50 |
| F12 | 20 |
| F13-F14 | 50 each |
| Total | 460 |

The runner records the protocol version and matrix SHA-256 in each run
manifest. The runner does not
disrupt a Docker daemon; the container scenario records a deterministic
fail-closed probe and must be repeated on explicit Linux/Windows CI hosts for
platform evidence.
