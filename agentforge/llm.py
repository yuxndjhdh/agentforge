"""OpenAI 兼容模型封装。

DeepSeek 推理模型（thinking mode）不接受显式 `tool_choice`，这里在生成时剥掉
（默认 auto）。这是 agent-eval-harness 里已验证能跑通 DeepSeek 的关键接线。
"""

from __future__ import annotations

import time

from .config import ModelConfig
from .context import compact_messages
from .observability import trace_span
from .tokenizer import build_token_counter

try:
    from smolagents import OpenAIServerModel
except Exception as _e:  # pragma: no cover
    OpenAIServerModel = None
    _IMPORT_ERROR: Exception | None = _e
else:
    _IMPORT_ERROR = None


class OpenAICompatServerModel(OpenAIServerModel):
    """剥掉显式 tool_choice（默认 auto），兼容 DeepSeek 等思维模型。"""

    def generate(
        self,
        messages,
        stop_sequences=None,
        response_format=None,
        tools_to_call_from=None,
        **kwargs,
    ):
        return super().generate(
            messages,
            stop_sequences=stop_sequences,
            response_format=response_format,
            tools_to_call_from=tools_to_call_from,
            tool_choice=None,
            **kwargs,
        )


class CompactingModel(OpenAICompatServerModel):
    """预算感知的上下文压缩模型：消息串超预算时折叠最旧步再调用底层模型。

    smolagents 每步都重跑 ``write_memory_to_messages()`` 并调 ``generate``，
    所以在这里包一层即可覆盖步生成 / 规划 / 总结；压缩为**请求级**，不改 memory.steps。
    """

    def __init__(
        self,
        *,
        max_context_chars: int,
        context_min_tail: int,
        context_compression_enabled: bool = True,
        max_context_tokens: int | None = None,
        token_counter=None,
        tokenizer_metadata: dict | None = None,
        event_sink=None,
        llm_retries: int = 2,
        llm_backoff: float = 0.5,
        **kwargs,
    ):
        self.llm_retries = max(0, int(llm_retries))
        self.llm_backoff = max(0.0, float(llm_backoff))
        super().__init__(**kwargs)
        self.max_context_chars = max_context_chars
        self.context_min_tail = context_min_tail
        self.context_compression_enabled = bool(context_compression_enabled)
        self.max_context_tokens = max_context_tokens
        self.token_counter = token_counter
        self.tokenizer_metadata = dict(tokenizer_metadata or {})
        self.event_sink = event_sink
        self.compressions: list[dict] = []  # 每次实际触发的压缩统计
        self.compression_stats: list[dict] = []  # 每次请求的状态，包含 disabled/within_budget

    def generate(
        self,
        messages,
        stop_sequences=None,
        response_format=None,
        tools_to_call_from=None,
        **kwargs,
    ):
        compacted, stats = compact_messages(
            messages,
            self.max_context_chars,
            self.context_min_tail,
            token_counter=self.token_counter,
            max_tokens=self.max_context_tokens,
            tokenizer_name=self.tokenizer_metadata.get("name"),
            tokenizer_estimated=self.tokenizer_metadata.get("estimated"),
            enabled=self.context_compression_enabled,
        )
        self.compression_stats.append(stats)
        if stats["compressed"]:
            self.compressions.append(stats)
        # 不在此传 tool_choice：父类 OpenAICompatServerModel.generate 已负责剥 tool_choice，
        # 避免经 **kwargs 传到它时重复关键字。
        last_error: Exception | None = None
        for attempt in range(self.llm_retries + 1):
            with trace_span(
                "agentforge.llm_request",
                model=getattr(self, "model_id", ""),
                attempt=attempt + 1,
            ):
                try:
                    return super().generate(
                        compacted,
                        stop_sequences=stop_sequences,
                        response_format=response_format,
                        tools_to_call_from=tools_to_call_from,
                        **kwargs,
                    )
                except Exception as exc:
                    last_error = exc
                    if attempt >= self.llm_retries or not _retryable(exc):
                        if self.event_sink:
                            self.event_sink({"phase": "llm_failed", "attempt": attempt + 1, "error": str(exc)})
                        raise
                    if self.event_sink:
                        self.event_sink({"phase": "llm_retry", "attempt": attempt + 1, "error": str(exc)})
                    delay = self.llm_backoff * (2**attempt)
                    if delay:
                        time.sleep(delay)
        if last_error is not None:
            raise last_error
        raise RuntimeError("model generation ended without a result")  # pragma: no cover


def build_model(cfg: ModelConfig, *, event_sink=None):
    """用配置构建通向 OpenAI 兼容端点的模型。缺 key 时给出明确报错。"""
    if OpenAIServerModel is None:
        raise RuntimeError('需要安装 smolagents：pip install "smolagents[openai]"') from _IMPORT_ERROR
    if not cfg.api_key:
        raise ValueError("缺少 HARNESS_LLM_KEY（DeepSeek key 或网关 token）。")
    token_counter, tokenizer_metadata = build_token_counter(cfg.model, cfg.tokenizer)
    return CompactingModel(
        model_id=cfg.model,
        api_base=cfg.base_url,
        api_key=cfg.api_key,
        temperature=cfg.temperature,
        max_context_chars=cfg.max_context_chars,
        context_min_tail=cfg.context_min_tail,
        context_compression_enabled=cfg.context_compression_enabled,
        max_context_tokens=cfg.max_context_tokens,
        token_counter=token_counter,
        tokenizer_metadata=tokenizer_metadata,
        event_sink=event_sink,
        llm_retries=cfg.llm_retries,
        llm_backoff=cfg.llm_backoff,
        retry=False,
        client_kwargs={
            "timeout": cfg.llm_timeout,
            "max_retries": 0,
        },
    )


def _retryable(error: Exception) -> bool:
    """只对网络、超时、限流和 5xx 类错误重试。"""
    name = type(error).__name__.lower()
    text = str(error).lower()
    if any(token in name for token in ("timeout", "connection", "ratelimit")):
        return True
    if any(token in text for token in ("timed out", "timeout", "429", "rate limit", "502", "503", "504")):
        return True
    status = getattr(error, "status_code", None)
    return status == 429 or (isinstance(status, int) and status >= 500)
