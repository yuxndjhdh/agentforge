"""分层记忆：working / daily / durable 三层，落盘为工作目录下的 JSON。

- **working**：smolagents 的 memory.steps（循环内的事中记忆），进程内、现成，不落盘。
- **daily**：`<workdir>/.agentforge/memory/daily-<YYYY-MM-DD>.json`，当天工作笔记。
- **durable**：`<workdir>/.agentforge/memory/durable.json`，跨会话项目知识；启动时注入系统提示。

本模块纯文件 JSON，无 smolagents 依赖，可独立单测。记忆目录以 `.` 开头，故不会被
`eval.hash_tree` 计入评测（hash_tree 忽略 `.` 前缀目录）。
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass
from datetime import date
from typing import Any

MEMORY_DIR = ".agentforge/memory"
TIERS = ("durable", "daily")

_CONTENT_MAX = 4000  # 单条 content 上限字符
_SEARCH_LIMIT = 20
_SNIPPET = 120
_DURABLE_INJECT_MAX_ENTRIES = 30
_DURABLE_INJECT_MAX_CHARS = 3000

_DATE_RE = re.compile(r"^daily-(\d{4}-\d{2}-\d{2})\.json$")


def _now_ts() -> float:
    return time.time()


@dataclass
class MemoryEntry:
    tier: str
    key: str
    content: str
    updated: float


class MemoryStore:
    """按 workdir 管理分层记忆的 JSON 文件。路径不存在时按空处理，不算错误。"""

    def __init__(self, workdir: str):
        self.root = os.path.join(os.path.realpath(workdir), MEMORY_DIR)
        self.durable_path = os.path.join(self.root, "durable.json")

    def _daily_path(self) -> str:
        return os.path.join(self.root, f"daily-{date.today().isoformat()}.json")

    def _read(self, path: str) -> dict[str, dict]:
        try:
            data = json.loads(open(path, encoding="utf-8").read())
        except (OSError, ValueError):
            return {}
        if not isinstance(data, dict):
            return {}
        return data

    def _write(self, path: str, data: dict[str, dict]) -> None:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    def save(self, tier: str, key: str, content: str) -> str:
        if tier not in TIERS:
            return f"Error: 未知 tier {tier!r}（可选 {TIERS}）"
        if not key or not key.strip():
            return "Error: key 不能为空"
        if tier == "durable":
            path = self.durable_path
        else:
            path = self._daily_path()
        data = self._read(path)
        data[key.strip()] = {"content": content[:_CONTENT_MAX], "updated": _now_ts()}
        self._write(path, data)
        return f"ok {tier}/{key.strip()}"

    def search(self, query: str, limit: int = _SEARCH_LIMIT) -> str:
        needle = query.strip().lower()
        if not needle:
            return "(query 不能为空)"
        results: list[str] = []
        hit = self._scan(needle, lambda tier, key, content: f"- [{tier}] {key}: {content[:_SNIPPET]}")
        for line in hit[:limit]:
            results.append(line)
        return "\n".join(results) if results else "(无匹配记忆)"

    def _scan(self, needle: str, fmt):
        """遍历 durable + 全部 daily 文件，命中返回按 fmt 格式化。"""
        out = []
        for tier, path in self._files():
            for key, meta in self._read(path).items():
                content = str(meta.get("content", ""))
                if needle in key.lower() or needle in content.lower():
                    out.append(fmt(tier, key, content))
        return out

    def _files(self):
        files = [("durable", self.durable_path)]
        try:
            names = sorted(os.listdir(self.root))
        except OSError:
            return files
        for n in names:
            m = _DATE_RE.match(n)
            if m:
                files.append(("daily", os.path.join(self.root, n)))
        return files

    def durable_text(self) -> str:
        data = self._read(self.durable_path)
        lines = []
        used = 0
        for key, meta in list(data.items())[:_DURABLE_INJECT_MAX_ENTRIES]:
            text = f"- {key}: {str(meta.get('content', ''))}"
            if used + len(text) > _DURABLE_INJECT_MAX_CHARS:
                break
            lines.append(text)
            used += len(text)
        if not lines:
            return ""
        return "[持久记忆 (durable)]\n" + "\n".join(lines)


def memory_instructions(workdir: str) -> str:
    """启动注入用：durable 记忆文本（无则空串）。供 make_agent 作为 `instructions`。"""
    return MemoryStore(workdir).durable_text()
