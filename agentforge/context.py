"""上下文压缩：预算感知的请求级消息压缩（确定性摘要）。

smolagents 每步通过 ``write_memory_to_messages()`` 重新从 ``memory.steps`` 组装完整消息串，
再调 ``model.generate(messages)``。这里在模型层包一层：当消息串超过 ``max_chars`` 预算时，
把**最早几步**折叠成一条规则生成的摘要（工具名 + 观察片段），保留最近整步与 system prompt。

本模块为纯函数，不依赖 smolagents，可独立单测。压缩为**每次请求级**：不改动 ``memory.steps``，
符合「本次请求控制 token」的目标；每步重新压缩，最旧内容逐步让位于摘要。

关键约束：一个 step 在 memory 里是 ``[assistant(thought) -> tool_call -> user(入参) -> tool_response(观察)]``，
必须**整步丢弃**（不拆 assistant↔observation 配对），否则模型会拿到「有返回无调用」的残缺上下文。
"""

from __future__ import annotations

from typing import Any, Callable

try:  # smolagents 通常已安装；缺省时退化为轻量替身，便于隔离测试
    from smolagents.models import ChatMessage, MessageRole
except Exception:  # pragma: no cover
    ChatMessage = None
    MessageRole = None

# 一个 step 里读取的观察/想法给摘要时截断的长度
_DIGEST_OBS_CHARS = 160


def _role(m: Any) -> str:
    """消息角色字符串（MessageRole 是 str-enum，取 .value；否则回退 str）。"""
    r = getattr(m, "role", None)
    value = getattr(r, "value", None)
    return str(value if value is not None else (r or "")).replace("MessageRole.", "").lower()


def _msg_text(m: Any) -> str:
    """抽取一条消息的纯文本。content 是 [{"type","text"}...] 或 str。"""
    content = getattr(m, "content", None)
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    parts = []
    for item in content:
        if isinstance(item, dict):
            parts.append(str(item.get("text", "")))
        elif isinstance(item, str):
            parts.append(item)
        else:
            parts.append(str(item))
    return "".join(parts)


def count_chars(messages: list) -> int:
    """消息串总字符数（预算单位用字符，避免引入 tiktoken / 与 DeepSeek tokenizer 不匹配）。"""
    return sum(len(_msg_text(m)) for m in messages)


def _tool_name(m: Any) -> str:
    """从 assistant 消息里取第一个工具调用名（无工具调用返回空）。"""
    tcs = getattr(m, "tool_calls", None) or []
    if tcs:
        fn = getattr(tcs[0], "function", None)
        return getattr(fn, "name", "") or ""
    return ""


def _step_ranges(messages: list) -> tuple[int, list[tuple[int, int]]]:
    """切分 prelude 与整步区间。

    prelude = 索引0（system）+ 开头连续 USER 消息（任务/目标，永不丢弃）。
    之后按 assistant 消息切分整步区间：每步止于下一个 assistant 之前（或末尾）。
    返回 ``(prelude_count, [(start, end), ...])``，end 为开区间。
    """
    prelude = 0
    if messages:
        prelude = 1  # system
        # 紧跟在 system 后的连续 user 消息属于任务上下文
        i = 1
        while i < len(messages) and _role(messages[i]) == "user":
            prelude = i + 1
            i += 1
    ranges: list[tuple[int, int]] = []
    start = prelude
    for i in range(prelude, len(messages)):
        if _role(messages[i]) == "assistant" and i != prelude:
            ranges.append((start, i))
            start = i
    if start < len(messages):
        ranges.append((start, len(messages)))
    return prelude, ranges


def _digest_size(dropped: int) -> int:
    """丢弃 dropped 步时摘要的大致字符数（预算估算用，实际以生成后 count 为准）。"""
    if dropped <= 0:
        return 0
    head = len("[上下文已压缩 · 最早 N 步折叠为摘要]\n")
    return head + dropped * (len("- <tool_name>: ") + _DIGEST_OBS_CHARS + 1)


def _digest(lines: list[str]) -> str:
    """把被丢弃的步拼成一条带前缀的摘要文本。"""
    head = "[上下文已压缩 · 最早 N 步折叠为摘要]\n"
    return head + "\n".join(lines)


def _step_digest(msgs: list, start: int, end: int) -> str:
    """为单个被丢弃的步构造摘要行：`- <tool_name>: <观察前160字符>`。"""
    # 先看这个步里有没有工具调用（assistant 首条）
    first = msgs[start]
    tool = _tool_name(first)
    # 观察/产出：取本步内最后一个非空文本（一般是 tool_response 的观察）
    obs = ""
    for m in msgs[start:end]:
        text = _msg_text(m)
        if text:
            obs = text
    snippet = obs.strip().replace("\n", " ")[:_DIGEST_OBS_CHARS]
    label = tool if tool else "thought"
    return f"- {label}: {snippet}"


def compact_messages(
    messages: list,
    max_chars: int,
    min_tail_steps: int = 4,
    *,
    token_counter: Callable[[list], int] | None = None,
    max_tokens: int | None = None,
    tokenizer_name: str | None = None,
    tokenizer_estimated: bool | None = None,
    enabled: bool = True,
) -> tuple[list, dict]:
    """超字符或 token 预算则折叠最旧步；不修改入参。

    ``token_counter`` 是可插拔的 provider tokenizer；未提供时只使用字符
    预算。输入、输出 token 会记录到 stats，便于 benchmark 做真实成本统计。
    ``enabled=False`` 保持原消息不变，但仍记录一次 ``disabled`` 状态，便于
    benchmark 区分“没有超预算”和“实验明确关闭压缩”。
    """
    in_chars = count_chars(messages)
    counter = token_counter
    used_fallback = False
    if counter is None and max_tokens is not None:
        # A deterministic fallback is useful when a provider tokenizer is not
        # installed; reports should treat this as an estimate.
        def counter(items):
            return max(1, count_chars(items) // 4)
        used_fallback = True
    if tokenizer_name is None and used_fallback:
        tokenizer_name = "chars-div-4"
    if tokenizer_estimated is None and used_fallback:
        tokenizer_estimated = True
    in_tokens = counter(messages) if counter else None
    stats = {
        "compressed": False,
        "compression_enabled": bool(enabled),
        "status": "within_budget" if enabled else "disabled",
        "in_chars": in_chars,
        "out_chars": in_chars,
        "saved_chars": 0,
        "dropped_steps": 0,
        "kept_steps": 0,
        "boundary_is_assistant": bool(messages and _role(messages[-1]) == "assistant"),
        "in_tokens": in_tokens,
        "out_tokens": in_tokens,
        "saved_tokens": 0,
        "max_tokens": max_tokens,
        "budget_source": "tokens" if max_tokens is not None else "characters",
        "tokenizer": tokenizer_name,
        "token_estimated": tokenizer_estimated,
    }
    if not enabled:
        return messages, stats
    within_tokens = max_tokens is None or (in_tokens is not None and in_tokens <= max_tokens)
    if (in_chars <= max_chars and within_tokens) or not messages:
        return messages, stats

    prelude, ranges = _step_ranges(messages)
    if not ranges:
        # 只有 prelude（system + 任务），没有可丢的整步——原样返回
        stats["status"] = "over_budget_uncompressible"
        return messages, stats

    def build_candidate(keep_count: int) -> tuple[list, list[tuple[int, int]], bool]:
        if keep_count > 0:
            dropped_ranges = ranges[:-keep_count] if keep_count < len(ranges) else []
            kept_ranges = ranges[-keep_count:]
        else:
            dropped_ranges = ranges
            kept_ranges = []
        candidate = list(messages[:prelude])
        digest_lines = [_step_digest(messages, start, end) for start, end in dropped_ranges]
        dropped_chars = sum(count_chars(messages[s:e]) for s, e in dropped_ranges)
        digest_text = _digest(digest_lines) if digest_lines else ""
        inserted = bool(digest_text and len(digest_text) < dropped_chars)
        if inserted:
            candidate.append(_make_user_message(digest_text))
        for start, end in kept_ranges:
            candidate.extend(messages[start:end])
        return candidate, dropped_ranges, inserted

    # Find the largest recent tail that satisfies both budgets. The actual
    # digest is counted, so a token budget cannot be exceeded by its summary.
    floor = min(len(ranges), max(0, min_tail_steps))
    chosen: tuple[list, list[tuple[int, int]], bool] | None = None
    for keep_count in range(len(ranges) - 1, floor - 1, -1):
        candidate, dropped_ranges, inserted = build_candidate(keep_count)
        candidate_tokens = counter(candidate) if counter else None
        if count_chars(candidate) <= max_chars and (
            max_tokens is None or candidate_tokens is None or candidate_tokens <= max_tokens
        ):
            chosen = (candidate, dropped_ranges, inserted)
            break
    if chosen is None and floor < len(ranges):
        chosen = build_candidate(floor)
    if chosen is None:
        stats["status"] = "over_budget_uncompressible"
        return messages, stats

    kept_msgs, dropped, inserted = chosen
    if not dropped:
        # 历史太短（步数不大于 min_tail）无法安全折叠——保持原样，不算触发压缩
        stats["status"] = "over_budget_uncompressible"
        return messages, stats

    out_chars = count_chars(kept_msgs)
    out_tokens = counter(kept_msgs) if counter else None
    # 摘要后 / prelude 后的第一条必须是 assistant（整步起点），保证 assistant↔observation 配对完好
    first_kept_step = kept_msgs[prelude + (1 if inserted else 0) :]
    boundary_is_assistant = bool(first_kept_step and _role(first_kept_step[0]) == "assistant")

    stats.update(
        {
            "compressed": True,
            "status": (
                "compressed"
                if out_chars <= max_chars and (max_tokens is None or out_tokens is None or out_tokens <= max_tokens)
                else "over_budget_uncompressible"
            ),
            "out_chars": out_chars,
            "saved_chars": max(0, in_chars - out_chars),
            "dropped_steps": len(dropped),
            "kept_steps": len(ranges) - len(dropped),
            "boundary_is_assistant": boundary_is_assistant,
            "out_tokens": out_tokens,
            "saved_tokens": (
                max(0, in_tokens - out_tokens)
                if in_tokens is not None and out_tokens is not None
                else 0
            ),
        }
    )
    return kept_msgs, stats


class _SimpleUser:
    """轻量消息替身：仅用于重组后的 USER 摘要，带 role/content，可被 count_chars 读取。"""

    def __init__(self, text: str):
        self.role = "user"
        self.content = [{"type": "text", "text": text}]
        self.tool_calls = None


def _make_user_message(text: str) -> Any:
    """构造一条 USER 消息（尽量用真实 ChatMessage 保证与 OpenAI 序列化兼容）。"""
    if ChatMessage is not None:
        return ChatMessage(role=MessageRole.USER, content=[{"type": "text", "text": text}])
    return _SimpleUser(text)
