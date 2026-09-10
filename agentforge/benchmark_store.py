"""Durable benchmark job state shared by API service instances."""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS benchmark_jobs (
    id TEXT PRIMARY KEY,
    status TEXT NOT NULL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    trials INTEGER NOT NULL,
    k INTEGER NOT NULL,
    seed INTEGER NOT NULL,
    tasks_json TEXT NOT NULL,
    config_json TEXT NOT NULL,
    report_path TEXT,
    error TEXT
);
CREATE INDEX IF NOT EXISTS idx_benchmark_jobs_created ON benchmark_jobs(created_at);
"""


class BenchmarkStore:
    """Small SQLite repository for benchmark lifecycle state."""

    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        if str(self.db_path) != ":memory:":
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(
            str(self.db_path), check_same_thread=False, isolation_level=None
        )
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA journal_mode=WAL")
        self._connection.execute("PRAGMA synchronous=FULL")
        self._connection.executescript(SCHEMA)
        self._lock = threading.RLock()

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def create(self, record: dict[str, Any]) -> dict[str, Any]:
        now = time.time()
        with self._lock:
            self._connection.execute(
                """INSERT INTO benchmark_jobs
                (id, status, created_at, updated_at, trials, k, seed, tasks_json,
                 config_json, report_path, error)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    record["id"],
                    record.get("status", "running"),
                    record.get("created_at", now),
                    record.get("updated_at", now),
                    int(record["trials"]),
                    int(record["k"]),
                    int(record.get("seed", 0)),
                    json.dumps(record.get("tasks", []), ensure_ascii=False),
                    json.dumps(record.get("config", {}), ensure_ascii=False),
                    record.get("report_path"),
                    record.get("error"),
                ),
            )
        return self.get(record["id"]) or dict(record)

    def get(self, job_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM benchmark_jobs WHERE id = ?", (job_id,)
            ).fetchone()
        return _record(row) if row else None

    def update(self, job_id: str, **changes: Any) -> dict[str, Any] | None:
        allowed = {"status", "report_path", "error", "config", "tasks", "trials", "k", "seed"}
        fields = {key: value for key, value in changes.items() if key in allowed}
        if "config" in fields:
            fields["config_json"] = json.dumps(fields.pop("config"), ensure_ascii=False)
        if "tasks" in fields:
            fields["tasks_json"] = json.dumps(fields.pop("tasks"), ensure_ascii=False)
        fields["updated_at"] = time.time()
        assignments = ", ".join(f"{key} = ?" for key in fields)
        with self._lock:
            self._connection.execute(
                f"UPDATE benchmark_jobs SET {assignments} WHERE id = ?",
                (*fields.values(), job_id),
            )
        return self.get(job_id)

    def recover_running(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT id FROM benchmark_jobs WHERE status = 'running'"
            ).fetchall()
        recovered = []
        for row in rows:
            updated = self.update(
                str(row["id"]),
                status="failed",
                error="benchmark worker stopped during service restart",
            )
            if updated:
                recovered.append(updated)
        return recovered


def _record(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "status": row["status"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "trials": row["trials"],
        "k": row["k"],
        "seed": row["seed"],
        "tasks": _load(row["tasks_json"], []),
        "config": _load(row["config_json"], {}),
        "report_path": row["report_path"],
        "error": row["error"],
    }


def _load(value: str, default: Any) -> Any:
    try:
        loaded = json.loads(value)
    except (TypeError, ValueError):
        return default
    return loaded
