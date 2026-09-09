"""环境变量驱动的模型配置。

与 agent-eval-harness 共用 `HARNESS_LLM_BASE` / `HARNESS_LLM_MODEL` / `HARNESS_LLM_KEY`
一套约定，方便复用 DeepSeek key 或网关 token。key 留空表示未配置（selftest 不需要）。
"""

from __future__ import annotations

import os
from dataclasses import dataclass

DEFAULT_BASE = "https://api.deepseek.com"
DEFAULT_MODEL = "deepseek-v4-flash"


@dataclass
class ModelConfig:
    base_url: str
    api_key: str
    model: str
    temperature: float = 0.0
    max_steps: int = 20
    max_context_chars: int = 45000
    context_min_tail: int = 4
    sandbox_whitelist: str = ""        # 逗号分隔，空=不启用白名单
    sandbox_denylist: str = ""         # 逗号分隔，追加到默认拒绝首 token 列表
    sandbox_deny_patterns: str = ""    # 逗号分隔，追加到默认拒绝模式
    sandbox_readonly: bool = False
    sandbox_timeout: float = 30.0


def load_config() -> ModelConfig:
    """读取环境变量（若存在 .env 则加载）。"""
    try:
        from dotenv import load_dotenv

        load_dotenv()
    except Exception:  # python-dotenv 非必装
        pass
    return ModelConfig(
        base_url=os.environ.get("HARNESS_LLM_BASE", DEFAULT_BASE),
        api_key=os.environ.get("HARNESS_LLM_KEY", ""),
        model=os.environ.get("HARNESS_LLM_MODEL", DEFAULT_MODEL),
        temperature=float(os.environ.get("HARNESS_LLM_TEMPERATURE", "0.0")),
        max_steps=int(os.environ.get("HARNESS_MAX_STEPS", "20")),
        max_context_chars=int(os.environ.get("HARNESS_MAX_CONTEXT_CHARS", "45000")),
        context_min_tail=int(os.environ.get("HARNESS_CONTEXT_MIN_TAIL", "4")),
        sandbox_whitelist=os.environ.get("HARNESS_SANDBOX_WHITELIST", ""),
        sandbox_denylist=os.environ.get("HARNESS_SANDBOX_DENYLIST", ""),
        sandbox_deny_patterns=os.environ.get("HARNESS_SANDBOX_DENY_PATTERNS", ""),
        sandbox_readonly=os.environ.get("HARNESS_SANDBOX_READONLY", "0") == "1",
        sandbox_timeout=float(os.environ.get("HARNESS_SANDBOX_TIMEOUT", "30.0")),
    )
