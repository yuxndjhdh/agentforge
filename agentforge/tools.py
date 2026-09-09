"""代码域工具：read_file / write_file / list_dir / grep / run_command。

安全说明（MVP）：
- 文件工具用 workdir + 真实路径收敛做**基础防护**（防路径穿越 / 越界写入），
  参数非法时返回 `Error: ...` 观测而非抛崩，agent 可据观测自行纠正。
- `run_command` 目前仅"workdir 下子进程 + 超时"，**不是沙箱**。工具级沙箱
  （白名单、权限、隔离环境）是后续迭代点。见 README「已知要点与坑」。
"""

from __future__ import annotations

import os
import re
import subprocess
from typing import Any

from smolagents import Tool

from .memory import MemoryStore, TIERS
from .sandbox import Sandbox

READ_LIMIT = 6000
GREP_LIMIT = 200
OUTPUT_LIMIT = 4000


def _resolve(workdir: str, relpath: str | None) -> str | None:
    """把相对路径解析到 workdir 内的真实路径；越界返回 None。"""
    base = os.path.realpath(workdir)
    target = os.path.realpath(os.path.join(base, relpath or "."))
    if target != base and not target.startswith(base + os.sep):
        return None
    return target


class _CodeTool(Tool):
    output_type = "string"

    def __init__(self, workdir: str, sandbox: Sandbox | None = None):
        super().__init__()
        self.workdir = os.path.realpath(workdir)
        self.sandbox = sandbox or Sandbox.default()

    def _resolve(self, rel: str | None) -> str | None:
        return _resolve(self.workdir, rel)


class ReadFileTool(_CodeTool):
    name = "read_file"
    description = "读取仓库内某个文本文件的内容（超过 6000 字符会截断）。path 为相对工作目录的路径。"
    inputs = {"path": {"type": "string", "description": "要读取的相对文件路径"}}

    def forward(self, path: str) -> str:
        rp = self._resolve(path)
        if rp is None:
            return "Error: path escapes workdir"
        if not os.path.isfile(rp):
            return f"Error: no such file: {path}"
        data = open(rp, encoding="utf-8", errors="replace").read()
        if len(data) <= READ_LIMIT:
            return data
        return data[:READ_LIMIT] + f"\n...(truncated {len(data) - READ_LIMIT} chars)"


class WriteFileTool(_CodeTool):
    name = "write_file"
    description = "把文本内容覆盖写入仓库内的一个文件（自动创建父目录）。path 不得越出工作目录。"
    inputs = {
        "path": {"type": "string", "description": "要写入的相对文件路径"},
        "content": {"type": "string", "description": "要写入的完整内容"},
    }

    def forward(self, path: str, content: str) -> str:
        err = self.sandbox.check_write()
        if err:
            return err
        rp = self._resolve(path)
        if rp is None:
            return "Error: path escapes workdir"
        os.makedirs(os.path.dirname(rp) or ".", exist_ok=True)
        with open(rp, "w", encoding="utf-8") as f:
            f.write(content)
        return f"wrote {len(content)} chars to {path}"


class ListDirTool(_CodeTool):
    name = "list_dir"
    description = "列出工作目录下某个目录里的条目（文件名/子目录名）。path 缺省为当前目录。"
    inputs = {"path": {"type": "string", "description": "相对目录路径，缺省 .", "nullable": True}}

    def forward(self, path: str = ".") -> str:
        rp = self._resolve(path)
        if rp is None:
            return "Error: path escapes workdir"
        if not os.path.isdir(rp):
            return f"Error: not a directory: {path}"
        entries = sorted(e.name for e in os.scandir(rp))
        return "\n".join(entries[:200]) or "(empty)"


class GrepTool(_CodeTool):
    name = "grep"
    description = (
        "在工作目录内按正则搜索文本，返回 '相对路径:行号: 内容'。pattern 用 Python 正则语法。"
    )
    inputs = {
        "pattern": {"type": "string", "description": "Python 正则表达式"},
        "path": {"type": "string", "description": "相对目录路径，缺省当前目录", "nullable": True},
    }

    def forward(self, pattern: str, path: str = ".") -> str:
        root = self._resolve(path)
        if root is None:
            return "Error: path escapes workdir"
        if not os.path.isdir(root):
            return f"Error: not a directory: {path}"
        try:
            rx = re.compile(pattern)
        except re.error as e:
            return f"Error: bad pattern: {e}"
        results: list[str] = []
        for dirpath, dirs, files in os.walk(root):
            dirs[:] = [d for d in dirs if d not in (".git", ".venv", "__pycache__", "node_modules", ".agentforge")]
            for fname in files:
                fp = os.path.join(dirpath, fname)
                try:
                    text = open(fp, encoding="utf-8", errors="replace").read()
                except OSError:
                    continue
                for i, line in enumerate(text.splitlines(), 1):
                    if rx.search(line):
                        rel = os.path.relpath(fp, root)
                        results.append(f"{rel}:{i}: {line.strip()[:200]}")
                        if len(results) >= GREP_LIMIT:
                            return "\n".join(results) + f"\n...(limited to {GREP_LIMIT})"
        return "\n".join(results) or "(no matches)"


class RunCommandTool(_CodeTool):
    name = "run_command"
    description = (
        "在工作目录下执行一条 shell 命令并返回 stdout/stderr 与退出码。有沙箱校验 + 超时保护，"
        "默认拒绝 rm/curl/wget/sudo/外联 等危险命令。"
    )
    inputs = {"command": {"type": "string", "description": "要执行的 shell 命令"}}

    def forward(self, command: str) -> str:
        err = self.sandbox.check_write()
        if err:
            return err
        err = self.sandbox.check_command(command)
        if err:
            return err
        try:
            cp = subprocess.run(
                command,
                shell=True,
                cwd=self.workdir,
                capture_output=True,
                text=True,
                env=self.sandbox.env(),
                timeout=self.sandbox.timeout,
            )
        except subprocess.TimeoutExpired:
            return f"Error: command timed out after {self.sandbox.timeout:g}s"
        except Exception as e:
            return f"Error: {type(e).__name__}: {e}"
        out = cp.stdout or ""
        err = cp.stderr or ""
        body = out if not err else out + "\n--- stderr ---\n" + err
        body = body[:OUTPUT_LIMIT]
        return f"(exit={cp.returncode})\n{body}"


class MemorySaveTool(_CodeTool):
    name = "memory_save"
    description = (
        "保存一条记忆到分层记忆库。tier=durable（跨会话长期项目知识，启动会注入到系统提示；"
        "跨进程仍记得）或 daily（当天工作笔记，靠 memory_search 拉取）。key 为简短标识，content 为要点。"
    )
    inputs = {
        "tier": {"type": "string", "description": f"tier：{' / '.join(TIERS)}"},
        "key": {"type": "string", "description": "简短标识名"},
        "content": {"type": "string", "description": "要记住的要点内容"},
    }

    def forward(self, tier: str, key: str, content: str) -> str:
        err = self.sandbox.check_write()
        if err:
            return err
        return MemoryStore(self.workdir).save(tier, key, content)


class MemorySearchTool(_CodeTool):
    name = "memory_search"
    description = "在分层记忆库按关键词搜索（durable + 全部 daily），返回 'key: 内容片段'，无命中则说明。"
    inputs = {"query": {"type": "string", "description": "要搜索的关键词"}}

    def forward(self, query: str) -> str:
        return MemoryStore(self.workdir).search(query)


def all_code_tools(workdir: str, sandbox: Sandbox | None = None) -> list[Tool]:
    """按固定顺序返回一组绑定到 workdir 的代码工具。

    sandbox 可注入工具级沙箱（默认 Sandbox.default()）。传 None 时不共享实例，
    各工具各自取默认——因此自定义沙箱应显式传入（如 make_agent 用 Sandbox.from_config）。
    """
    return [
        ReadFileTool(workdir),
        WriteFileTool(workdir, sandbox),
        ListDirTool(workdir),
        GrepTool(workdir),
        RunCommandTool(workdir, sandbox),
        MemorySaveTool(workdir, sandbox),
        MemorySearchTool(workdir),
    ]
