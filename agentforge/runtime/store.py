"""Durable runtime metadata and append-only event storage."""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Iterable

from .models import (
    CHECKPOINT_SCHEMA_VERSION,
    Attempt,
    AttemptStatus,
    Checkpoint,
    Run,
    RunStatus,
    RuntimeEvent,
    Step,
    ToolCall,
    Verification,
    stable_id,
)

SCHEMA = """
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS runs (
    id TEXT PRIMARY KEY,
    task TEXT NOT NULL,
    workdir TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    model TEXT NOT NULL DEFAULT '',
    project_id TEXT NOT NULL DEFAULT 'default',
    user_id TEXT NOT NULL DEFAULT 'default',
    max_steps INTEGER NOT NULL,
    max_duration REAL,
    attempt_id TEXT,
    error TEXT,
    answer TEXT,
    cancel_requested INTEGER NOT NULL DEFAULT 0,
    metadata_json TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS attempts (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    number INTEGER NOT NULL,
    status TEXT NOT NULL,
    created_at REAL NOT NULL,
    started_at REAL,
    finished_at REAL,
    checkpoint_seq INTEGER NOT NULL DEFAULT 0,
    error TEXT,
    answer TEXT,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    UNIQUE(run_id, number)
);
CREATE TABLE IF NOT EXISTS steps (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    attempt_id TEXT NOT NULL REFERENCES attempts(id) ON DELETE CASCADE,
    number INTEGER NOT NULL,
    kind TEXT NOT NULL,
    status TEXT NOT NULL,
    started_at REAL NOT NULL,
    finished_at REAL,
    input_json TEXT,
    output_json TEXT,
    error TEXT,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    UNIQUE(attempt_id, number, kind)
);
CREATE TABLE IF NOT EXISTS tool_calls (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    attempt_id TEXT NOT NULL REFERENCES attempts(id) ON DELETE CASCADE,
    step_id TEXT REFERENCES steps(id) ON DELETE SET NULL,
    name TEXT NOT NULL,
    arguments_json TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL,
    started_at REAL NOT NULL,
    finished_at REAL,
    observation TEXT,
    error TEXT,
    idempotency_key TEXT UNIQUE
);
CREATE TABLE IF NOT EXISTS verifications (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    attempt_id TEXT NOT NULL REFERENCES attempts(id) ON DELETE CASCADE,
    status TEXT NOT NULL,
    started_at REAL NOT NULL,
    finished_at REAL,
    reward INTEGER,
    checks_json TEXT NOT NULL DEFAULT '[]',
    feedback TEXT NOT NULL DEFAULT '',
    error TEXT
);
CREATE TABLE IF NOT EXISTS checkpoints (
    run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    attempt_id TEXT REFERENCES attempts(id) ON DELETE CASCADE,
    sequence INTEGER NOT NULL,
    created_at REAL NOT NULL,
    state_json TEXT NOT NULL,
    schema_version INTEGER NOT NULL DEFAULT 1,
    complete INTEGER NOT NULL DEFAULT 0,
    is_event INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY(run_id, sequence)
);
CREATE TABLE IF NOT EXISTS event_counters (
    run_id TEXT PRIMARY KEY REFERENCES runs(id) ON DELETE CASCADE,
    next_sequence INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS checkpoint_counters (
    run_id TEXT PRIMARY KEY REFERENCES runs(id) ON DELETE CASCADE,
    next_sequence INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_attempts_run ON attempts(run_id, number);
CREATE INDEX IF NOT EXISTS idx_steps_run ON steps(run_id, number);
CREATE INDEX IF NOT EXISTS idx_tool_calls_run ON tool_calls(run_id, started_at);
CREATE INDEX IF NOT EXISTS idx_events_run ON checkpoints(run_id, sequence);
"""


class JsonlEventStore:
    """Per-run append-only JSONL events with tolerant partial-tail recovery."""

    def __init__(self, root: str | Path):
        self.root = Path(root)
        self._lock = threading.RLock()

    def path(self, run_id: str) -> Path:
        return self.root / run_id / "events.jsonl"

    def append(self, event: RuntimeEvent | dict[str, Any]) -> dict[str, Any]:
        record = event.to_record() if isinstance(event, RuntimeEvent) else dict(event)
        path = self.path(str(record["run_id"]))
        with self._lock:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8", newline="\n") as stream:
                stream.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")
                stream.flush()
                os.fsync(stream.fileno())
        return record

    def read(self, run_id: str, *, after_sequence: int = 0) -> list[dict[str, Any]]:
        path = self.path(run_id)
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return []
        result = []
        for line in lines:
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except ValueError:
                # A process can be killed after writing a partial final line.
                continue
            if isinstance(record, dict) and int(record.get("sequence", 0)) > after_sequence:
                result.append(record)
        return result

    def export(self, run_id: str, records: list[dict[str, Any]], path: str | Path | None = None) -> Path:
        target = Path(path) if path else self.root / run_id / "trace.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=".agentforge-trace-", dir=target.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
                json.dump(records, stream, ensure_ascii=False, indent=2)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, target)
        finally:
            if os.path.exists(temporary):
                try:
                    os.unlink(temporary)
                except OSError:
                    pass
        return target


class RuntimeStore:
    """SQLite metadata store plus append-only events for one AgentForge root."""

    def __init__(self, db_path: str | Path = ".agentforge/state.sqlite3", event_root: str | Path = "runs"):
        self.db_path = Path(db_path)
        if str(self.db_path) != ":memory:":
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.events = JsonlEventStore(event_root)
        self._lock = threading.RLock()
        self._connection = sqlite3.connect(
            str(self.db_path),
            check_same_thread=False,
            isolation_level=None,
        )
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA journal_mode=WAL")
        self._connection.execute("PRAGMA synchronous=FULL")
        self._connection.executescript(SCHEMA)
        self._ensure_checkpoint_schema()

    def _ensure_checkpoint_schema(self) -> None:
        """Migrate state databases created before checkpoint schemas existed."""
        columns = {
            str(row["name"])
            for row in self._connection.execute("PRAGMA table_info(checkpoints)").fetchall()
        }
        if "schema_version" not in columns:
            self._connection.execute(
                "ALTER TABLE checkpoints ADD COLUMN schema_version INTEGER NOT NULL DEFAULT 1"
            )

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def create_run(self, run: Run) -> Run:
        record = run.to_record()
        with self._lock:
            self._connection.execute(
                """INSERT INTO runs
                (id, task, workdir, status, created_at, updated_at, model, project_id,
                 user_id, max_steps, max_duration, attempt_id, error, answer,
                 cancel_requested, metadata_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    record["id"], record["task"], record["workdir"], record["status"],
                    record["created_at"], record["updated_at"], record["model"],
                    record["project_id"], record["user_id"], record["max_steps"],
                    record["max_duration"], record["attempt_id"], record["error"],
                    record["answer"], int(record["cancel_requested"]), _dump(record["metadata"]),
                ),
            )
        return run

    def get_run(self, run_id: str) -> Run | None:
        row = self._one("SELECT * FROM runs WHERE id = ?", (run_id,))
        return _run_from_row(row) if row else None

    def list_runs(self, *, limit: int = 100) -> list[Run]:
        rows = self._connection.execute(
            "SELECT * FROM runs ORDER BY created_at DESC LIMIT ?", (max(1, limit),)
        ).fetchall()
        return [_run_from_row(row) for row in rows]

    def update_run(self, run_id: str, **changes: Any) -> Run | None:
        allowed = {
            "status", "updated_at", "model", "attempt_id", "error", "answer",
            "cancel_requested", "metadata",
        }
        fields = {key: value for key, value in changes.items() if key in allowed}
        fields.setdefault("updated_at", __import__("time").time())
        if "metadata" in fields:
            fields["metadata_json"] = _dump(fields.pop("metadata"))
        if "status" in fields:
            fields["status"] = getattr(fields["status"], "value", fields["status"])
        if "cancel_requested" in fields:
            fields["cancel_requested"] = int(bool(fields["cancel_requested"]))
        assignments = ", ".join(f"{key} = ?" for key in fields)
        with self._lock:
            self._connection.execute(
                f"UPDATE runs SET {assignments} WHERE id = ?",
                (*fields.values(), run_id),
            )
        return self.get_run(run_id)

    def create_attempt(self, attempt: Attempt) -> Attempt:
        record = attempt.to_record()
        with self._lock:
            self._connection.execute(
                """INSERT INTO attempts
                (id, run_id, number, status, created_at, started_at, finished_at,
                 checkpoint_seq, error, answer, metadata_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    record["id"], record["run_id"], record["number"], record["status"],
                    record["created_at"], record["started_at"], record["finished_at"],
                    record["checkpoint_seq"], record["error"], record["answer"],
                    _dump(record["metadata"]),
                ),
            )
            self.update_run(attempt.run_id, attempt_id=attempt.id)
        return attempt

    def get_attempt(self, attempt_id: str) -> Attempt | None:
        row = self._one("SELECT * FROM attempts WHERE id = ?", (attempt_id,))
        return _attempt_from_row(row) if row else None

    def list_attempts(self, run_id: str) -> list[Attempt]:
        rows = self._connection.execute(
            "SELECT * FROM attempts WHERE run_id = ? ORDER BY number", (run_id,)
        ).fetchall()
        return [_attempt_from_row(row) for row in rows]

    def update_attempt(self, attempt_id: str, **changes: Any) -> Attempt | None:
        allowed = {
            "status", "started_at", "finished_at", "checkpoint_seq", "error", "answer", "metadata",
        }
        fields = {key: value for key, value in changes.items() if key in allowed}
        if "metadata" in fields:
            fields["metadata_json"] = _dump(fields.pop("metadata"))
        if "status" in fields:
            fields["status"] = getattr(fields["status"], "value", fields["status"])
        if not fields:
            return self.get_attempt(attempt_id)
        assignments = ", ".join(f"{key} = ?" for key in fields)
        with self._lock:
            self._connection.execute(
                f"UPDATE attempts SET {assignments} WHERE id = ?", (*fields.values(), attempt_id)
            )
        return self.get_attempt(attempt_id)

    def append_step(self, step: Step) -> Step:
        record = step.to_record()
        with self._lock:
            self._connection.execute(
                """INSERT OR REPLACE INTO steps
                (id, run_id, attempt_id, number, kind, status, started_at, finished_at,
                 input_json, output_json, error, metadata_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    record["id"], record["run_id"], record["attempt_id"], record["number"],
                    record["kind"], record["status"], record["started_at"], record["finished_at"],
                    _dump(record["input"]), _dump(record["output"]), record["error"],
                    _dump(record["metadata"]),
                ),
            )
        return step

    def list_steps(self, run_id: str, attempt_id: str | None = None) -> list[Step]:
        if attempt_id:
            rows = self._connection.execute(
                "SELECT * FROM steps WHERE run_id = ? AND attempt_id = ? ORDER BY number, started_at",
                (run_id, attempt_id),
            ).fetchall()
        else:
            rows = self._connection.execute(
                "SELECT * FROM steps WHERE run_id = ? ORDER BY started_at, number", (run_id,)
            ).fetchall()
        return [_step_from_row(row) for row in rows]

    def append_tool_call(self, call: ToolCall) -> ToolCall:
        record = call.to_record()
        with self._lock:
            self._connection.execute(
                """INSERT INTO tool_calls
                (id, run_id, attempt_id, step_id, name, arguments_json, status,
                 started_at, finished_at, observation, error, idempotency_key)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    record["id"], record["run_id"], record["attempt_id"], record["step_id"],
                    record["name"], _dump(record["arguments"]), record["status"],
                    record["started_at"], record["finished_at"], record["observation"],
                    record["error"], record["idempotency_key"],
                ),
            )
        return call

    def get_tool_call_by_key(self, key: str) -> ToolCall | None:
        row = self._one(
            """SELECT * FROM tool_calls
               WHERE idempotency_key = ? OR idempotency_key LIKE ?
               ORDER BY CASE WHEN idempotency_key = ? THEN 0 ELSE 1 END, started_at DESC
               LIMIT 1""",
            (key, f"{key}:retry:%", key),
        )
        return _tool_call_from_row(row) if row else None

    def next_tool_retry_key(self, key: str) -> str:
        """Return a unique key that remains discoverable through the base key."""
        return f"{key}:retry:{stable_id('toolretry')}"

    def update_tool_call(self, call_id: str, **changes: Any) -> ToolCall | None:
        allowed = {"step_id", "status", "finished_at", "observation", "error"}
        fields = {key: value for key, value in changes.items() if key in allowed}
        if "status" in fields:
            fields["status"] = getattr(fields["status"], "value", fields["status"])
        if not fields:
            return self._get_tool_call(call_id)
        assignments = ", ".join(f"{key} = ?" for key in fields)
        with self._lock:
            self._connection.execute(
                f"UPDATE tool_calls SET {assignments} WHERE id = ?", (*fields.values(), call_id)
            )
        return self._get_tool_call(call_id)

    def list_tool_calls(self, run_id: str) -> list[ToolCall]:
        rows = self._connection.execute(
            "SELECT * FROM tool_calls WHERE run_id = ? ORDER BY started_at", (run_id,)
        ).fetchall()
        return [_tool_call_from_row(row) for row in rows]

    def append_verification(self, verification: Verification) -> Verification:
        record = verification.to_record()
        with self._lock:
            self._connection.execute(
                """INSERT OR REPLACE INTO verifications
                (id, run_id, attempt_id, status, started_at, finished_at, reward,
                 checks_json, feedback, error)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    record["id"], record["run_id"], record["attempt_id"], record["status"],
                    record["started_at"], record["finished_at"], record["reward"],
                    _dump(record["checks"]), record["feedback"], record["error"],
                ),
            )
        return verification

    def list_verifications(self, run_id: str) -> list[Verification]:
        rows = self._connection.execute(
            "SELECT * FROM verifications WHERE run_id = ? ORDER BY started_at", (run_id,)
        ).fetchall()
        return [_verification_from_row(row) for row in rows]

    def save_checkpoint(self, checkpoint: Checkpoint) -> Checkpoint:
        if checkpoint.schema_version != CHECKPOINT_SCHEMA_VERSION:
            raise CheckpointSchemaError(
                f"unsupported checkpoint schema version: {checkpoint.schema_version}"
            )
        if not isinstance(checkpoint.state, dict):
            raise CheckpointSchemaError("checkpoint state must be an object")
        record = checkpoint.to_record()
        with self._lock:
            self._connection.execute(
                """INSERT OR REPLACE INTO checkpoints
                (run_id, attempt_id, sequence, created_at, state_json, schema_version, complete, is_event)
                VALUES (?, ?, ?, ?, ?, ?, ?, 0)""",
                (
                    record["run_id"], record["attempt_id"], record["sequence"], record["created_at"],
                    _dump(record["state"]), record["schema_version"], int(record["complete"]),
                ),
            )
            self._connection.execute(
                """INSERT INTO checkpoint_counters(run_id, next_sequence) VALUES (?, ?)
                   ON CONFLICT(run_id) DO UPDATE SET next_sequence =
                   MAX(checkpoint_counters.next_sequence, excluded.next_sequence)""",
                (checkpoint.run_id, checkpoint.sequence + 1),
            )
            if checkpoint.attempt_id is not None:
                self.update_attempt(checkpoint.attempt_id, checkpoint_seq=checkpoint.sequence)
        return checkpoint

    def latest_checkpoint(self, run_id: str, *, complete_only: bool = True) -> Checkpoint | None:
        query = "SELECT * FROM checkpoints WHERE run_id = ? AND is_event = 0"
        params: list[Any] = [run_id]
        if complete_only:
            query += " AND complete = 1"
        query += " ORDER BY sequence DESC"
        rows = self._connection.execute(query, tuple(params)).fetchall()
        for row in rows:
            try:
                return _checkpoint_from_row(row)
            except CheckpointCorruptionError:
                continue
        return None

    def next_checkpoint_sequence(self, run_id: str) -> int:
        with self._lock:
            existing = self._connection.execute(
                "SELECT COALESCE(MAX(sequence), 0) + 1 AS next FROM checkpoints WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            self._connection.execute(
                "INSERT OR IGNORE INTO checkpoint_counters(run_id, next_sequence) VALUES (?, ?)",
                (run_id, int(existing["next"]) if existing else 1),
            )
            row = self._connection.execute(
                "SELECT next_sequence FROM checkpoint_counters WHERE run_id = ?", (run_id,)
            ).fetchone()
        return int(row["next_sequence"]) if row else 1

    def next_sequence(self, run_id: str) -> int:
        """Backward-compatible alias for checkpoint sequence allocation."""
        return self.next_checkpoint_sequence(run_id)

    def append_event(
        self,
        run_id: str,
        event_type: str,
        *,
        attempt_id: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        with self._lock:
            existing_events = self.events.read(run_id)
            existing_max = max((int(item.get("sequence", 0)) for item in existing_events), default=0)
            self._connection.execute(
                "INSERT OR IGNORE INTO event_counters(run_id, next_sequence) VALUES (?, ?)",
                (run_id, existing_max + 1),
            )
            row = self._connection.execute(
                "SELECT next_sequence FROM event_counters WHERE run_id = ?", (run_id,)
            ).fetchone()
            sequence = max(int(row["next_sequence"]), existing_max + 1)
            event = RuntimeEvent.create(
                run_id,
                event_type,
                sequence,
                attempt_id=attempt_id,
                payload=payload,
            )
            record = self.events.append(event)
            self._connection.execute(
                "UPDATE event_counters SET next_sequence = ? WHERE run_id = ?",
                (sequence + 1, run_id),
            )
        return record

    def recover_stale_runs(self) -> list[Run]:
        """Mark runs left active by a dead process as failed but resumable."""
        with self._lock:
            rows = self._connection.execute(
                "SELECT * FROM runs WHERE status IN (?, ?) ORDER BY created_at",
                (RunStatus.RUNNING.value, RunStatus.VERIFYING.value),
            ).fetchall()
        recovered: list[Run] = []
        for row in rows:
            run = _run_from_row(row)
            checkpoint = self.latest_checkpoint(run.id)
            metadata = dict(run.metadata)
            metadata.update({"resumable": True, "recovered_from_status": run.status.value})
            recovery_error = "runtime process stopped before completion; resume is available"
            if run.attempt_id is not None:
                self.update_attempt(
                    run.attempt_id,
                    status=AttemptStatus.FAILED,
                    finished_at=time.time(),
                    error=recovery_error,
                )
            updated = self.update_run(
                run.id,
                status=RunStatus.FAILED,
                error=recovery_error,
                metadata=metadata,
            )
            if updated is not None:
                self.append_event(
                    run.id,
                    "run.recovered",
                    attempt_id=run.attempt_id,
                    payload={
                        "previous_status": run.status.value,
                        "checkpoint_sequence": checkpoint.sequence if checkpoint else None,
                        "resumable": True,
                    },
                )
                recovered.append(updated)
        return recovered

    def _one(self, query: str, params: Iterable[Any]) -> sqlite3.Row | None:
        with self._lock:
            return self._connection.execute(query, tuple(params)).fetchone()

    def _get_tool_call(self, call_id: str) -> ToolCall | None:
        row = self._one("SELECT * FROM tool_calls WHERE id = ?", (call_id,))
        return _tool_call_from_row(row) if row else None


def _dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _load(value: str | None, default: Any) -> Any:
    if value is None:
        return default
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


def _run_from_row(row: sqlite3.Row) -> Run:
    return Run(
        id=row["id"], task=row["task"], workdir=row["workdir"],
        status=RunStatus(row["status"]), created_at=row["created_at"], updated_at=row["updated_at"],
        model=row["model"], project_id=row["project_id"], user_id=row["user_id"],
        max_steps=row["max_steps"], max_duration=row["max_duration"], attempt_id=row["attempt_id"],
        error=row["error"], answer=row["answer"], cancel_requested=bool(row["cancel_requested"]),
        metadata=_load(row["metadata_json"], {}),
    )


def _attempt_from_row(row: sqlite3.Row) -> Attempt:
    return Attempt(
        id=row["id"], run_id=row["run_id"], number=row["number"], status=AttemptStatus(row["status"]),
        created_at=row["created_at"], started_at=row["started_at"], finished_at=row["finished_at"],
        checkpoint_seq=row["checkpoint_seq"], error=row["error"], answer=row["answer"],
        metadata=_load(row["metadata_json"], {}),
    )


def _step_from_row(row: sqlite3.Row) -> Step:
    from .models import StepStatus

    return Step(
        id=row["id"], run_id=row["run_id"], attempt_id=row["attempt_id"], number=row["number"],
        kind=row["kind"], status=StepStatus(row["status"]), started_at=row["started_at"],
        finished_at=row["finished_at"], input=_load(row["input_json"], None),
        output=_load(row["output_json"], None), error=row["error"], metadata=_load(row["metadata_json"], {}),
    )


def _tool_call_from_row(row: sqlite3.Row) -> ToolCall:
    from .models import StepStatus

    return ToolCall(
        id=row["id"], run_id=row["run_id"], attempt_id=row["attempt_id"], step_id=row["step_id"],
        name=row["name"], arguments=_load(row["arguments_json"], {}), status=StepStatus(row["status"]),
        started_at=row["started_at"], finished_at=row["finished_at"], observation=row["observation"],
        error=row["error"], idempotency_key=row["idempotency_key"],
    )


def _verification_from_row(row: sqlite3.Row) -> Verification:
    from .models import VerificationStatus

    return Verification(
        id=row["id"], run_id=row["run_id"], attempt_id=row["attempt_id"],
        status=VerificationStatus(row["status"]), started_at=row["started_at"],
        finished_at=row["finished_at"], reward=row["reward"], checks=_load(row["checks_json"], []),
        feedback=row["feedback"], error=row["error"],
    )


def _checkpoint_from_row(row: sqlite3.Row) -> Checkpoint:
    schema_version = int(row["schema_version"])
    if schema_version != CHECKPOINT_SCHEMA_VERSION:
        raise CheckpointSchemaError(f"unsupported checkpoint schema version: {schema_version}")
    state = _load_strict(row["state_json"])
    if not isinstance(state, dict):
        raise CheckpointCorruptionError("checkpoint state is not an object")
    return Checkpoint(
        run_id=row["run_id"], attempt_id=row["attempt_id"], sequence=row["sequence"],
        created_at=row["created_at"], state=state, schema_version=schema_version,
        complete=bool(row["complete"]),
    )


class CheckpointCorruptionError(ValueError):
    """A checkpoint row cannot be decoded and should be skipped during recovery."""


class CheckpointSchemaError(ValueError):
    """A checkpoint was written by an incompatible runtime version."""


def _load_strict(value: str | None) -> Any:
    if value is None:
        raise CheckpointCorruptionError("checkpoint state is missing")
    try:
        return json.loads(value)
    except (TypeError, ValueError) as exc:
        raise CheckpointCorruptionError("checkpoint state is invalid JSON") from exc
