"""代码域工具：read_file / write_file / list_dir / grep / run_command。

所有文件访问共用 `paths.resolve_path`，拒绝路径穿越、符号链接和写入硬链接；
命令通过 `Sandbox.execute` 以 argv 方式运行，并有进程组、超时和输出上限。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import time
import uuid

from smolagents import Tool

from .memory import TIERS, MemoryStore
from .paths import resolve_path
from .sandbox import Sandbox

READ_LIMIT = 6000
GREP_LIMIT = 200
OUTPUT_LIMIT = 4000


def _resolve(workdir: str, relpath: str | None) -> str | None:
    """把相对路径解析到 workdir 内；非法或链接路径返回 None。"""
    return resolve_path(workdir, relpath, allow_missing=True, reject_symlink=True).path


class _CodeTool(Tool):
    output_type = "string"

    def __init__(
        self,
        workdir: str,
        sandbox: Sandbox | None = None,
        *,
        project_id: str = "default",
        user_id: str = "default",
        run_id: str | None = None,
    ):
        super().__init__()
        self.workdir = os.path.realpath(workdir)
        self.sandbox = sandbox or Sandbox.default()
        self.project_id = project_id
        self.user_id = user_id
        self.run_id = run_id

    def _resolve(self, rel: str | None) -> str | None:
        return _resolve(self.workdir, rel)


class ReadFileTool(_CodeTool):
    name = "read_file"
    description = "读取仓库内某个文本文件的内容（超过 6000 字符会截断）。path 为相对工作目录的路径。"
    inputs = {"path": {"type": "string", "description": "要读取的相对文件路径"}}

    def forward(self, path: str) -> str:
        result = resolve_path(
            self.workdir,
            path,
            allow_missing=False,
            reject_symlink=True,
            reject_hardlink=True,
        )
        rp = result.path
        if rp is None:
            if result.reason == "path does not exist":
                return f"Error: no such file: {path}"
            return "Error: path escapes workdir"
        if not os.path.isfile(rp):
            return f"Error: no such file: {path}"
        try:
            data = open(rp, encoding="utf-8", errors="replace").read()
        except OSError as e:
            return f"Error: cannot read {path}: {e}"
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
        rp = resolve_path(
            self.workdir,
            path,
            allow_missing=True,
            reject_symlink=True,
            reject_hardlink=True,
        ).path
        if rp is None:
            return "Error: path escapes workdir"
        parent = os.path.dirname(rp) or self.workdir
        try:
            os.makedirs(parent, exist_ok=True)
            # 同目录临时文件 + replace 避免半写入文件；再次解析目标防止
            # 目录在检查后被替换为符号链接。
            checked = resolve_path(
                self.workdir,
                path,
                allow_missing=True,
                reject_symlink=True,
                reject_hardlink=True,
            ).path
            if checked is None:
                return "Error: path escapes workdir"
            fd, tmp = tempfile.mkstemp(prefix=".agentforge-write-", dir=parent)
            try:
                with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
                    f.write(content)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(tmp, checked)
            finally:
                if os.path.exists(tmp):
                    os.unlink(tmp)
        except OSError as e:
            return f"Error: cannot write {path}: {e}"
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
        try:
            entries = sorted(e.name for e in os.scandir(rp) if not os.path.islink(e.path))
        except OSError as e:
            return f"Error: cannot list {path}: {e}"
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
            dirs[:] = [
                d
                for d in dirs
                if d not in (".git", ".venv", "__pycache__", "node_modules", ".agentforge")
                and not os.path.islink(os.path.join(dirpath, d))
            ]
            for fname in files:
                fp = os.path.join(dirpath, fname)
                if os.path.islink(fp):
                    continue
                safe = resolve_path(
                    self.workdir,
                    os.path.relpath(fp, self.workdir),
                    allow_missing=False,
                    reject_symlink=True,
                    reject_hardlink=True,
                )
                if safe.path is None:
                    continue
                try:
                    text = open(safe.path, encoding="utf-8", errors="replace").read()
                except OSError:
                    continue
                for i, line in enumerate(text.splitlines(), 1):
                    if rx.search(line):
                        rel = os.path.relpath(safe.path, root).replace(os.sep, "/")
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
        result = self.sandbox.execute(command, cwd=self.workdir)
        if result.error:
            return result.error if result.error.startswith("Error:") else f"Error: {result.error}"
        if result.timed_out:
            return f"Error: command timed out after {self.sandbox.timeout:g}s"
        out = result.stdout or ""
        err = result.stderr or ""
        body = out if not err else out + "\n--- stderr ---\n" + err
        if result.output_limited:
            body += "\n...(output limited by sandbox)"
        body = body[:OUTPUT_LIMIT]
        return f"(exit={result.returncode})\n{body}"


class MemorySaveTool(_CodeTool):
    name = "memory_save"
    description = (
        "保存一条记忆到分层记忆库。durable/daily 内容都会作为不可信数据处理；"
        "kind 可选 fact、note、instruction，其中 instruction 不会自动注入系统提示。"
    )
    inputs = {
        "tier": {"type": "string", "description": f"tier：{' / '.join(TIERS)}"},
        "key": {"type": "string", "description": "简短标识名"},
        "content": {"type": "string", "description": "要记住的要点内容"},
        "kind": {"type": "string", "description": "记忆类型：fact / note / instruction", "nullable": True},
    }

    def forward(self, tier: str, key: str, content: str, kind: str = "fact") -> str:
        err = self.sandbox.check_write()
        if err:
            return err
        return MemoryStore(
            self.workdir,
            project_id=self.project_id,
            user_id=self.user_id,
            run_id=self.run_id,
        ).save(tier, key, content, kind=kind)


class MemorySearchTool(_CodeTool):
    name = "memory_search"
    description = (
        "在当前 project/user/run scope 的 durable、daily 和 run-local 记忆中按关键词搜索，"
        "返回 'key: 内容片段'，无命中则说明。"
    )
    inputs = {"query": {"type": "string", "description": "要搜索的关键词"}}

    def forward(self, query: str) -> str:
        return MemoryStore(
            self.workdir,
            project_id=self.project_id,
            user_id=self.user_id,
            run_id=self.run_id,
        ).search(query)


class _InstrumentedTool(Tool):
    """将工具调用以 start/end 事件形式发送给 runtime。"""

    skip_forward_signature_validation = True

    def __init__(self, inner: Tool, event_sink):
        self.name = inner.name
        self.description = inner.description
        self.inputs = inner.inputs
        self.output_type = inner.output_type
        self.output_schema = getattr(inner, "output_schema", None)
        self.inner = inner
        self.event_sink = event_sink
        self.is_initialized = True

    def forward(self, *args, **kwargs):
        call_id = f"toolcall_{uuid.uuid4().hex}"
        arguments = kwargs.copy()
        if args:
            arguments["_args"] = list(args)
        identity = json.dumps(
            {"name": self.name, "arguments": arguments},
            sort_keys=True,
            ensure_ascii=False,
            default=str,
        )
        idempotency_key = hashlib.sha256(identity.encode("utf-8")).hexdigest()
        decision = self._emit(
            {
                "phase": "started",
                "call_id": call_id,
                "name": self.name,
                "arguments": arguments,
                "idempotency_key": idempotency_key,
            }
        )
        if isinstance(decision, dict) and (decision.get("replay") or decision.get("reject")):
            return str(decision.get("observation", ""))
        started = time.monotonic()
        try:
            result = self.inner.forward(*args, **kwargs)
        except Exception as exc:
            self._emit(
                {
                    "phase": "completed",
                    "call_id": call_id,
                    "name": self.name,
                    "arguments": arguments,
                    "idempotency_key": idempotency_key,
                    "error": f"{type(exc).__name__}: {exc}",
                    "duration_ms": round((time.monotonic() - started) * 1000, 2),
                }
            )
            raise
        self._emit(
            {
                "phase": "completed",
                "call_id": call_id,
                "name": self.name,
                "arguments": arguments,
                "idempotency_key": idempotency_key,
                "observation": str(result)[:4000],
                "duration_ms": round((time.monotonic() - started) * 1000, 2),
            }
        )
        return result

    def _emit(self, event: dict):
        try:
            return self.event_sink(event)
        except Exception:
            # Instrumentation must not change tool semantics.
            return None


def all_code_tools(
    workdir: str,
    sandbox: Sandbox | None = None,
    *,
    event_sink=None,
    project_id: str = "default",
    user_id: str = "default",
    run_id: str | None = None,
) -> list[Tool]:
    """按固定顺序返回一组绑定到 workdir 的代码工具。

    sandbox 可注入工具级沙箱（默认 Sandbox.default()）。传 None 时不共享实例，
    各工具各自取默认——因此自定义沙箱应显式传入（如 make_agent 用 Sandbox.from_config）。
    """
    tools: list[Tool] = [
        ReadFileTool(workdir, project_id=project_id, user_id=user_id, run_id=run_id),
        WriteFileTool(workdir, sandbox),
        ListDirTool(workdir, project_id=project_id, user_id=user_id, run_id=run_id),
        GrepTool(workdir, project_id=project_id, user_id=user_id, run_id=run_id),
        RunCommandTool(workdir, sandbox, project_id=project_id, user_id=user_id, run_id=run_id),
        MemorySaveTool(
            workdir,
            sandbox,
            project_id=project_id,
            user_id=user_id,
            run_id=run_id,
        ),
        MemorySearchTool(
            workdir,
            project_id=project_id,
            user_id=user_id,
            run_id=run_id,
        ),
    ]
    if event_sink is not None:
        return [_InstrumentedTool(tool, event_sink) for tool in tools]
    return tools
