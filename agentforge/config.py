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
    max_context_tokens: int | None = None
    context_min_tail: int = 4
    sandbox_whitelist: str = ""        # 逗号分隔，空=不启用白名单
    sandbox_denylist: str = ""         # 逗号分隔，追加到默认拒绝首 token 列表
    sandbox_deny_patterns: str = ""    # 逗号分隔，追加到默认拒绝模式
    sandbox_readonly: bool = False
    sandbox_timeout: float = 30.0
    sandbox_max_output_bytes: int = 256000
    sandbox_backend: str = "auto"  # auto | local | docker | podman
    sandbox_image: str = "python:3.13-slim"
    sandbox_network: bool = False
    sandbox_cpu_limit: float = 1.0
    sandbox_memory_limit_mb: int = 512
    sandbox_pids_limit: int = 128
    sandbox_disk_limit_mb: int = 1024
    llm_timeout: float = 120.0
    llm_retries: int = 2
    llm_backoff: float = 0.5
    input_cost_per_million: float = 0.0
    output_cost_per_million: float = 0.0
    state_db: str = ".agentforge/state.sqlite3"
    trace_dir: str = "runs"

    def __post_init__(self) -> None:
        if self.max_steps < 1:
            raise ValueError("max_steps must be >= 1")
        if self.max_context_chars < 1 or self.context_min_tail < 0:
            raise ValueError("context budgets must be non-negative")
        if self.sandbox_timeout <= 0 or self.sandbox_max_output_bytes < 1024:
            raise ValueError("sandbox timeout/output limits are invalid")
        if self.sandbox_backend not in {"auto", "local", "docker", "podman"}:
            raise ValueError("sandbox_backend must be auto, local, docker, or podman")
        if self.sandbox_cpu_limit <= 0 or self.sandbox_memory_limit_mb < 16:
            raise ValueError("sandbox resource limits are invalid")
        if self.sandbox_pids_limit < 1 or self.sandbox_disk_limit_mb < 0:
            raise ValueError("sandbox process/disk limits are invalid")
        if self.llm_timeout <= 0 or self.llm_retries < 0 or self.llm_backoff < 0:
            raise ValueError("LLM timeout/retry settings are invalid")
        if self.input_cost_per_million < 0 or self.output_cost_per_million < 0:
            raise ValueError("LLM cost rates cannot be negative")


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
        max_context_tokens=(
            int(os.environ["HARNESS_MAX_CONTEXT_TOKENS"])
            if os.environ.get("HARNESS_MAX_CONTEXT_TOKENS")
            else None
        ),
        sandbox_whitelist=os.environ.get("HARNESS_SANDBOX_WHITELIST", ""),
        sandbox_denylist=os.environ.get("HARNESS_SANDBOX_DENYLIST", ""),
        sandbox_deny_patterns=os.environ.get("HARNESS_SANDBOX_DENY_PATTERNS", ""),
        sandbox_readonly=os.environ.get("HARNESS_SANDBOX_READONLY", "0") == "1",
        sandbox_timeout=float(os.environ.get("HARNESS_SANDBOX_TIMEOUT", "30.0")),
        sandbox_max_output_bytes=int(os.environ.get("HARNESS_SANDBOX_MAX_OUTPUT_BYTES", "256000")),
        sandbox_backend=os.environ.get("HARNESS_SANDBOX_BACKEND", "auto").lower(),
        sandbox_image=os.environ.get("HARNESS_SANDBOX_IMAGE", "python:3.13-slim"),
        sandbox_network=os.environ.get("HARNESS_SANDBOX_NETWORK", "0") == "1",
        sandbox_cpu_limit=float(os.environ.get("HARNESS_SANDBOX_CPU", "1.0")),
        sandbox_memory_limit_mb=int(os.environ.get("HARNESS_SANDBOX_MEMORY_MB", "512")),
        sandbox_pids_limit=int(os.environ.get("HARNESS_SANDBOX_PIDS", "128")),
        sandbox_disk_limit_mb=int(os.environ.get("HARNESS_SANDBOX_DISK_MB", "1024")),
        llm_timeout=float(os.environ.get("HARNESS_LLM_TIMEOUT", "120.0")),
        llm_retries=int(os.environ.get("HARNESS_LLM_RETRIES", "2")),
        llm_backoff=float(os.environ.get("HARNESS_LLM_BACKOFF", "0.5")),
        input_cost_per_million=float(os.environ.get("HARNESS_INPUT_COST_PER_MILLION", "0")),
        output_cost_per_million=float(os.environ.get("HARNESS_OUTPUT_COST_PER_MILLION", "0")),
        state_db=os.environ.get("AGENTFORGE_STATE_DB", ".agentforge/state.sqlite3"),
        trace_dir=os.environ.get("AGENTFORGE_TRACE_DIR", "runs"),
    )
