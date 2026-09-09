"""RunTrace：一次 agent 运行的步骤记录，可 dump/load JSON 并渲染到终端。

MVP 里 trace 兼作 checkpoint：跑完后把 `agent.memory.steps` 的关键字段抽出来落盘。
断点续跑则由 `run(task, reset=False)` 在同一 agent 上继续（见 harness.Session）。
"""

from __future__ import annotations

import json
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

    @classmethod
    def from_smol_agent(cls, agent, *, run_id: str, workdir: str, task: str, model: str = ""):
        """从 smolagents agent 的 memory.steps 抽取精简步骤。"""
        trace = cls(run_id=run_id, workdir=workdir, task=task, model=model)
        trace.compression = list(getattr(getattr(agent, "model", None), "compressions", []) or [])
        memory = getattr(agent, "memory", None)
        for st in getattr(memory, "steps", []) or []:
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
            "run_id": self.run_id,
            "workdir": self.workdir,
            "task": self.task,
            "model": self.model,
            "steps": self.steps,
            "compression": self.compression,
        }

    def dump(self, path: str | Path) -> Path:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(self.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
        return p

    @classmethod
    def load(cls, path: str | Path) -> "RunTrace":
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return cls(
            run_id=data["run_id"],
            workdir=data["workdir"],
            task=data["task"],
            model=data.get("model", ""),
            steps=data.get("steps", []),
            compression=data.get("compression", []),
        )

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
