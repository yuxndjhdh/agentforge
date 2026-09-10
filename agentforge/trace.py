"""一次运行的可审计 trace。

``RunTrace`` 仍支持从 smolagents 抽取兼容视图，但抽取可以指定 memory
边界；新的 runtime 以增量事件为事实来源，最终 trace 只是一个原子导出的
查询结果。
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


def _compact(arguments: Any) -> str:
    if arguments is None:
        return ""
    if isinstance(arguments, str):
        s = arguments
    else:
        s = json.dumps(arguments, ensure_ascii=False)
    return s if len(s) <= 200 else s[:200] + "…"


def _stringify(value: Any) -> str:
    return "" if value is None else str(value)


@dataclass
class RunTrace:
    run_id: str
    workdir: str
    task: str
    model: str = ""
    steps: list[dict] = field(default_factory=list)
    compression: list[dict] = field(default_factory=list)
    compression_stats: list[dict] = field(default_factory=list)
    compression_enabled: bool | None = None
    status: str = "succeeded"
    attempt_id: str | None = None
    schema_version: int = 2
    events: list[dict] = field(default_factory=list)

    @classmethod
    def from_smol_agent(
        cls,
        agent,
        *,
        run_id: str,
        workdir: str,
        task: str,
        model: str = "",
        start_index: int = 0,
        end_index: int | None = None,
        status: str = "succeeded",
        attempt_id: str | None = None,
    ):
        """从指定 memory.steps 区间抽取精简步骤。"""
        trace = cls(
            run_id=run_id,
            workdir=workdir,
            task=task,
            model=model,
            status=status,
            attempt_id=attempt_id,
        )
        agent_model = getattr(agent, "model", None)
        trace.compression = list(getattr(agent_model, "compressions", []) or [])
        trace.compression_stats = list(getattr(agent_model, "compression_stats", []) or [])
        enabled = getattr(agent_model, "context_compression_enabled", None)
        trace.compression_enabled = bool(enabled) if enabled is not None else None
        memory = getattr(agent, "memory", None)
        steps = getattr(memory, "steps", []) or []
        for st in steps[max(0, start_index) : end_index]:
            kind = type(st).__name__
            if kind == "TaskStep":
                trace.steps.append({"kind": "task", "task": st.task})
            elif kind == "PlanningStep":
                trace.steps.append({"kind": "plan", "plan": st.plan})
            elif kind == "ActionStep":
                calls = [{"name": tc.name, "arguments": tc.arguments} for tc in (st.tool_calls or [])]
                trace.steps.append(
                    {
                        "kind": "action",
                        "step": st.step_number,
                        "tool_calls": calls,
                        "observation": _stringify(st.observations)[:2000],
                        "token_usage": {
                            "input": getattr(st.token_usage, "input_tokens", None),
                            "output": getattr(st.token_usage, "output_tokens", None),
                        },
                    }
                )
            elif kind == "FinalAnswerStep":
                trace.steps.append({"kind": "final", "output": st.output})
        return trace

    def add_manual(self, kind: str, **kw) -> None:
        """selftest / 无模型路径下手动追加一个步骤。"""
        self.steps.append({"kind": kind, **kw})

    def to_dict(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "run_id": self.run_id,
            "workdir": self.workdir,
            "task": self.task,
            "model": self.model,
            "status": self.status,
            "attempt_id": self.attempt_id,
            "steps": self.steps,
            "compression": self.compression,
            "compression_stats": self.compression_stats,
            "compression_enabled": self.compression_enabled,
            "events": self.events,
        }

    def dump(self, path: str | Path) -> Path:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary_name = tempfile.mkstemp(prefix=f".{p.name}.", dir=p.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
                stream.write(json.dumps(self.to_dict(), ensure_ascii=False, indent=2) + "\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary_name, p)
        finally:
            if os.path.exists(temporary_name):
                try:
                    os.unlink(temporary_name)
                except OSError:
                    pass
        return p

    @classmethod
    def load(cls, path: str | Path) -> "RunTrace":
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return cls(
            run_id=data["run_id"],
            workdir=data["workdir"],
            task=data["task"],
            model=data.get("model", ""),
            status=data.get("status", "succeeded"),
            attempt_id=data.get("attempt_id"),
            schema_version=int(data.get("schema_version", 1)),
            steps=data.get("steps", []),
            compression=data.get("compression", []),
            compression_stats=data.get("compression_stats", data.get("compression", [])),
            compression_enabled=data.get("compression_enabled"),
            events=data.get("events", []),
        )

    def add_event(self, event: dict) -> None:
        """追加一个已持久化的 runtime event 到导出视图。"""
        self.events.append(dict(event))

    def render(self) -> str:
        lines = [
            f"# Run {self.run_id}",
            f"workdir: {self.workdir}",
            f"task: {self.task}",
            f"model: {self.model}",
            "",
        ]
        for st in self.steps:
            kind = st.get("kind")
            if kind == "task":
                lines.append(f"[任务] {st.get('task')}")
            elif kind == "plan":
                lines.append(f"[规划] {st.get('plan')}")
            elif kind == "action":
                calls = " || ".join(
                    f"{c['name']}({_compact(c.get('arguments'))})" for c in st.get("tool_calls", [])
                )
                lines.append(f"[步骤 {st.get('step')}] {calls}")
                if st.get("observation"):
                    lines.append(f"      观察: {st['observation'][:300]}")
            elif kind == "final":
                lines.append(f"[结论] {_compact(st.get('output'))}")
        if self.compression:
            n = len(self.compression)
            total_saved = sum(c.get("saved_chars", 0) for c in self.compression)
            last = self.compression[-1]
            lines.append(
                f"[压缩] 共 {n} 次，累计省 {total_saved:,} 字符（最后 in={last.get('in_chars')} out={last.get('out_chars')}）"
            )
        return "\n".join(lines)
