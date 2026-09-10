# AgentForge Architecture

The runtime is the source of truth for lifecycle state. smolagents is an execution adapter, not a persistence layer.

```text
Task -> Run -> Attempt -> Step -> ToolCall
                         -> Verification
              -> SQLite metadata + JSONL events -> trace.json
```

`Run` owns the stable run ID, work directory, status, cancellation flag, limits, and final answer. An `Attempt` is one execution or retry. Every persisted `Step`, `ToolCall`, and `Verification` references both run and attempt IDs. Events are appended immediately and receive a monotonically increasing per-run sequence. A checkpoint stores the last complete application state separately from event sequence numbers.

The CLI and API construct `AgentRuntime`. `RuntimeContext` is the provider-neutral interface for checkpoints, steps, cancellation, and verification. The smolagents adapter snapshots only the current memory range and persists it as runtime steps. Instrumented tools emit start/end events without changing their return values.

The evaluator is intentionally outside the agent loop. It creates a fresh seed repository, runs authoritative checks, records every episode, and computes the standard combination estimator. Benchmark reports include configuration and failure traces so an experiment can be reproduced and audited.

Memory is scoped by project and user. Run-local entries are stored under the
current run ID and are never included in another run's query or export.
Durable memory is untrusted data; instruction-kind entries are excluded from
automatic prompt injection. Context compression uses a provider tokenizer
when available and records whether token counts are estimated.

For execution, the policy layer parses command arguments before the backend runs them. The local backend uses argv, a new process group, a timeout, and bounded output. The container backend mounts only the work directory, disables networking by default, uses a non-root UID, drops capabilities, and sets CPU, memory, PID, temporary storage, and disk limits.
