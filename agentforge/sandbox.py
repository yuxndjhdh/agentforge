"""工具级沙箱：白名单 + 默认拒绝命令/模式 + 读环境变量消杀 + 只读开关。

四道闸：
1. `check_command` —— 白名单（非空才启用）→ 默认拒绝首 token → 默认拒绝模式。
2. `check_write`   —— readonly 时全禁写（write_file / memory_save / run_command）。
3. `env`           —— 抹掉名字含 KEY/TOKEN/SECRET/PASSWORD/API_KEY 等的变量，防 agent 读到 LLM key。
4. `timeout`       —— run_command 子进程超时上限。

设计：**默认松（allow-anything-not-denied）**。白名单为空时只按 deny_commands + deny_patterns
拦截明显危险的东西，避免破坏既有工具链与测试；`readonly` 一键收紧。
沙箱是「工具注入 + 请求检查」，不改 smolagents 执行循环。
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field

SECRET_ENV = (
    "HARNESS_LLM_KEY",
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "API_KEY",
    "TOKEN",
    "SECRET",
    "PASSWORD",
    "KEY",
)

DEFAULT_DENY_COMMANDS = (
    "rm", "sudo", "curl", "wget", "nc", "ssh", "scp", "rsync", "git",
    "sh", "bash", "zsh", "pip", "apt", "yum", "dnf", "apk", "brew",
)

DEFAULT_DENY_PATTERNS = (
    r"\brm\s+-(rf|fr|r)\b",
    r"\bcurl\b",
    r"\bwget\b",
    r"\bsudo\b",
    r"\b(nc|netcat)\b",
    r"\bsh\s+-c\b",
    r"\bbash\s+-c\b",
    r"\bzsh\s+-c\b",
    r"\bpip\s+install\b",
    r"\bgit\s+(push|reset\s+--hard|clean\b|checkout\s+-f)",
    r"\bcd\s+(/|~)",
)


@dataclass
class Sandbox:
    whitelist: tuple[str, ...] = ()
    deny_commands: tuple[str, ...] = DEFAULT_DENY_COMMANDS
    deny_patterns: tuple[str, ...] = DEFAULT_DENY_PATTERNS
    readonly: bool = False
    timeout: float = 30.0

    def check_command(self, command: str) -> str | None:
        """校验命令是否放行；被拒返回 Error 文案，放行返回 None。"""
        if not command.strip():
            return "Error: empty command"
        first = command.strip().split()[0]
        if self.whitelist and not any(first == w or first.startswith(w + "/") for w in self.whitelist):
            return (
                f"Error: command '{first}' not in sandbox whitelist "
                f"(allowed: {', '.join(self.whitelist)})"
            )
        for dc in self.deny_commands:
            if first == dc:
                return f"Error: command '{dc}' is denied by sandbox"
        for p in self.deny_patterns:
            if re.search(p, command):
                return f"Error: command {command!r} matches sandbox deny pattern"
        return None

    def check_write(self) -> str | None:
        """readonly 时全禁写；放行返回 None。"""
        if self.readonly:
            return "Error: sandbox is readonly (write/run blocked)"
        return None

    def env(self, base: dict[str, str] | None = None) -> dict[str, str]:
        """拷贝环境并抹掉含敏感子串的变量，再打上 AGENTFORGE_SANDBOXED=1。"""
        env = dict(os.environ if base is None else base)
        for name in list(env):
            upper = name.upper()
            if any(secret in upper for secret in SECRET_ENV):
                del env[name]
        env["AGENTFORGE_SANDBOXED"] = "1"
        return env

    @classmethod
    def default(cls) -> "Sandbox":
        return cls()

    @classmethod
    def from_config(cls, cfg) -> "Sandbox":
        """从配置构建：denylist / deny_patterns 追加到默认之上，whitelist 空则保持空。"""
        whitelist = _split(cfg.sandbox_whitelist)
        denylist = _split(cfg.sandbox_denylist)
        deny_patterns = _split(cfg.sandbox_deny_patterns)
        return cls(
            whitelist=whitelist,
            deny_commands=DEFAULT_DENY_COMMANDS + tuple(denylist),
            deny_patterns=DEFAULT_DENY_PATTERNS + tuple(deny_patterns),
            readonly=cfg.sandbox_readonly,
            timeout=float(cfg.sandbox_timeout),
        )


def _split(s: str) -> tuple[str, ...]:
    return tuple(x.strip() for x in s.split(",") if x.strip())
