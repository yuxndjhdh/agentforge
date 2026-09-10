"""Stable runtime domain objects.

These objects intentionally do not depend on smolagents. They are the durable
contract between the CLI, API, evaluator, and any future execution backend.
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import Any

CHECKPOINT_SCHEMA_VERSION = 1


class RunStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    VERIFYING = "verifying"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class AttemptStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class StepStatus(StrEnum):
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class VerificationStatus(StrEnum):
    PENDING = "pending"
    PASSED = "passed"
    FAILED = "failed"


def utc_timestamp() -> float:
    return time.time()


def stable_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def _enum_value(value: Any) -> Any:
    return value.value if isinstance(value, StrEnum) else value


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


@dataclass
class Run:
    id: str
    task: str
    workdir: str
    status: RunStatus = RunStatus.PENDING
    created_at: float = field(default_factory=utc_timestamp)
    updated_at: float = field(default_factory=utc_timestamp)
    model: str = ""
    project_id: str = "default"
    user_id: str = "default"
    max_steps: int = 20
    max_duration: float | None = None
    attempt_id: str | None = None
    error: str | None = None
    answer: str | None = None
    cancel_requested: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def create(cls, task: str, workdir: str, **kwargs: Any) -> "Run":
        return cls(id=stable_id("run"), task=task, workdir=workdir, **kwargs)

    def to_record(self) -> dict[str, Any]:
        record = asdict(self)
        record["status"] = _enum_value(self.status)
        return record


@dataclass
class Attempt:
    id: str
    run_id: str
    number: int
    status: AttemptStatus = AttemptStatus.PENDING
    created_at: float = field(default_factory=utc_timestamp)
    started_at: float | None = None
    finished_at: float | None = None
    checkpoint_seq: int = 0
    error: str | None = None
    answer: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def create(cls, run_id: str, number: int, **kwargs: Any) -> "Attempt":
        return cls(id=stable_id("attempt"), run_id=run_id, number=number, **kwargs)

    def to_record(self) -> dict[str, Any]:
        record = asdict(self)
        record["status"] = _enum_value(self.status)
        return record


@dataclass
class Step:
    id: str
    run_id: str
    attempt_id: str
    number: int
    kind: str
    status: StepStatus = StepStatus.SUCCEEDED
    started_at: float = field(default_factory=utc_timestamp)
    finished_at: float | None = None
    input: Any = None
    output: Any = None
    error: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def create(cls, run_id: str, attempt_id: str, number: int, kind: str, **kwargs: Any) -> "Step":
        return cls(id=stable_id("step"), run_id=run_id, attempt_id=attempt_id, number=number, kind=kind, **kwargs)

    def to_record(self) -> dict[str, Any]:
        record = asdict(self)
        record["status"] = _enum_value(self.status)
        return record


@dataclass
class ToolCall:
    id: str
    run_id: str
    attempt_id: str
    step_id: str | None
    name: str
    arguments: dict[str, Any] = field(default_factory=dict)
    status: StepStatus = StepStatus.RUNNING
    started_at: float = field(default_factory=utc_timestamp)
    finished_at: float | None = None
    observation: str | None = None
    error: str | None = None
    idempotency_key: str | None = None

    @classmethod
    def create(
        cls,
        run_id: str,
        attempt_id: str,
        name: str,
        arguments: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> "ToolCall":
        call_id = stable_id("tool")
        return cls(
            id=call_id,
            run_id=run_id,
            attempt_id=attempt_id,
            step_id=kwargs.pop("step_id", None),
            name=name,
            arguments=arguments or {},
            idempotency_key=kwargs.pop("idempotency_key", call_id),
            **kwargs,
        )

    def to_record(self) -> dict[str, Any]:
        record = asdict(self)
        record["status"] = _enum_value(self.status)
        return record


@dataclass
class Verification:
    id: str
    run_id: str
    attempt_id: str
    status: VerificationStatus = VerificationStatus.PENDING
    started_at: float = field(default_factory=utc_timestamp)
    finished_at: float | None = None
    reward: int | None = None
    checks: list[dict[str, Any]] = field(default_factory=list)
    feedback: str = ""
    error: str | None = None

    @classmethod
    def create(cls, run_id: str, attempt_id: str, **kwargs: Any) -> "Verification":
        return cls(id=stable_id("verification"), run_id=run_id, attempt_id=attempt_id, **kwargs)

    def to_record(self) -> dict[str, Any]:
        record = asdict(self)
        record["status"] = _enum_value(self.status)
        return record


@dataclass
class Checkpoint:
    run_id: str
    attempt_id: str | None
    sequence: int
    created_at: float
    state: dict[str, Any]
    complete: bool = False
    schema_version: int = CHECKPOINT_SCHEMA_VERSION

    @classmethod
    def create(
        cls,
        run_id: str,
        attempt_id: str | None,
        sequence: int,
        state: dict[str, Any],
        *,
        complete: bool = False,
        schema_version: int = CHECKPOINT_SCHEMA_VERSION,
    ) -> "Checkpoint":
        return cls(run_id, attempt_id, sequence, utc_timestamp(), state, complete, schema_version)

    def to_record(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class RuntimeEvent:
    event_id: str
    run_id: str
    type: str
    timestamp: float
    sequence: int
    attempt_id: str | None = None
    payload: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def create(
        cls,
        run_id: str,
        event_type: str,
        sequence: int,
        *,
        attempt_id: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> "RuntimeEvent":
        return cls(
            event_id=stable_id("event"),
            run_id=run_id,
            type=event_type,
            timestamp=utc_timestamp(),
            sequence=sequence,
            attempt_id=attempt_id,
            payload=payload or {},
        )

    def to_record(self) -> dict[str, Any]:
        return asdict(self)
