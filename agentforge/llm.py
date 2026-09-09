"""OpenAI 兼容模型封装。

DeepSeek 推理模型（thinking mode）不接受显式 `tool_choice`，这里在生成时剥掉
（默认 auto）。这是 agent-eval-harness 里已验证能跑通 DeepSeek 的关键接线。
"""

from __future__ import annotations

from .config import ModelConfig
from .context import compact_messages

try:
    from smolagents import OpenAIServerModel
except Exception as _e:  # pragma: no cover
    OpenAIServerModel = None  # type: ignore
    _IMPORT_ERROR = _e
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

    def __init__(self, *, max_context_chars: int, context_min_tail: int, **kwargs):
        super().__init__(**kwargs)
        self.max_context_chars = max_context_chars
        self.context_min_tail = context_min_tail
        self.compressions: list[dict] = []  # 每次实际触发的压缩统计

    def generate(
        self,
        messages,
        stop_sequences=None,
        response_format=None,
        tools_to_call_from=None,
        **kwargs,
    ):
        compacted, stats = compact_messages(messages, self.max_context_chars, self.context_min_tail)
        if stats["compressed"]:
            self.compressions.append(stats)
        # 不在此传 tool_choice：父类 OpenAICompatServerModel.generate 已负责剥 tool_choice，
        # 避免经 **kwargs 传到它时重复关键字。
        return super().generate(
            compacted,
            stop_sequences=stop_sequences,
            response_format=response_format,
            tools_to_call_from=tools_to_call_from,
            **kwargs,
        )


def build_model(cfg: ModelConfig):
    """用配置构建通向 OpenAI 兼容端点的模型。缺 key 时给出明确报错。"""
    if OpenAIServerModel is None:
        raise RuntimeError('需要安装 smolagents：pip install "smolagents[openai]"') from _IMPORT_ERROR
    if not cfg.api_key:
        raise ValueError("缺少 HARNESS_LLM_KEY（DeepSeek key 或网关 token）。")
    return CompactingModel(
        model_id=cfg.model,
        api_base=cfg.base_url,
        api_key=cfg.api_key,
        temperature=cfg.temperature,
        max_context_chars=cfg.max_context_chars,
        context_min_tail=cfg.context_min_tail,
    )
