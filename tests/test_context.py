"""上下文压缩层单测（无需 LLM）：compact_messages 的预算/摘要/边界/保底。"""

from __future__ import annotations

from types import SimpleNamespace

from agentforge.context import _role, compact_messages, count_chars


def _system(text="SYSTEM PROMPT"):
    return SimpleNamespace(role="system", content=[{"type": "text", "text": text}], tool_calls=None)


def _user(text):
    return SimpleNamespace(role="user", content=[{"type": "text", "text": text}], tool_calls=None)


def _assistant(text, tool_name=None):
    tc = [SimpleNamespace(function=SimpleNamespace(name=tool_name))] if tool_name else None
    return SimpleNamespace(role="assistant", content=[{"type": "text", "text": text}], tool_calls=tc)


def _tool_call():
    return SimpleNamespace(role="tool_call", content=[{"type": "text", "text": "call"}], tool_calls=None)


def _tool_response(text):
    return SimpleNamespace(role="tool_response", content=[{"type": "text", "text": text}], tool_calls=None)


def _make_steps(n, obs_len=300, tool="read_file"):
    """system + 任务 + n 个 (assistant, tool_call, user, tool_response) 整步。"""
    msgs = [_system(), _user("TASK: fix the bug")]
    for i in range(n):
        msgs += [
            _assistant(f"thought {i}", tool),
            _tool_call(),
            _user(f"args {i}"),
            _tool_response(f"OBSERVATION_{i}" + "x" * obs_len),
        ]
    return msgs


def test_count_chars_counts_content():
    msgs = [_system("hello"), _user("world")]
    assert count_chars(msgs) == len("hello") + len("world")


def test_within_budget_passthrough():
    msgs = _make_steps(3, obs_len=100)
    out, stats = compact_messages(msgs, max_chars=count_chars(msgs) + 1, min_tail_steps=2)
    assert out is msgs  # 原样返回，不新建
    assert stats["compressed"] is False


def test_compression_can_be_disabled_without_changing_messages():
    msgs = _make_steps(12, obs_len=300)
    out, stats = compact_messages(msgs, max_chars=10, min_tail_steps=2, enabled=False)
    assert out is msgs
    assert stats["compression_enabled"] is False
    assert stats["status"] == "disabled"
    assert stats["compressed"] is False
    assert stats["saved_chars"] == 0


def test_compressed_drops_oldest_and_summarizes():
    msgs = _make_steps(12, obs_len=300)
    in_chars = count_chars(msgs)
    budget = 3000
    out, stats = compact_messages(msgs, max_chars=budget, min_tail_steps=3)
    assert stats["compressed"] is True
    assert stats["dropped_steps"] > 0
    assert stats["kept_steps"] >= 3
    assert stats["out_chars"] < in_chars
    assert stats["saved_chars"] == in_chars - stats["out_chars"]
    # system 与任务保留
    assert _role(out[0]) == "system"
    assert _role(out[1]) == "user"
    # 摘要（user）之后紧跟 assistant——配对完好
    assert _role(out[2]) == "user"
    assert _role(out[3]) == "assistant"
    assert stats["boundary_is_assistant"] is True


def test_min_tail_floor_keeps_recent_steps():
    msgs = _make_steps(12, obs_len=300)
    # 极小预算下也不会把最近步全压掉
    out, stats = compact_messages(msgs, max_chars=10, min_tail_steps=3)
    assert stats["kept_steps"] >= 3
    # 最后一次工具观察必须仍在（模型正要用的上下文不能丢）
    assert "OBSERVATION_11" in _msg_all(out)


def _msg_all(msgs):
    return "".join(m.content[0]["text"] for m in msgs if hasattr(m.content, "__getitem__"))


def test_no_dangling_tool_response():
    msgs = _make_steps(30, obs_len=500)
    out, stats = compact_messages(msgs, max_chars=2000, min_tail_steps=4)
    kept_start = 3 if _role(out[2]) == "user" else 2  # 摘要可能占一个 user
    roles = [_role(m) for m in out[kept_start:]]
    # 保留区第一个必须是 assistant（整步起点），绝不会是 tool_response 起头
    assert roles[0] == "assistant"
    # 完整整步：每对 assistant→tool_response 数量相等（无悬空）
    assert roles.count("assistant") == roles.count("tool_response")


def test_thought_only_small_steps_drop_without_inflating():
    # 纯 thought 小步：摘要比被丢内容还大，应纯截断（不插摘要），且绝不增大
    msgs = [_system(), _user("TASK")]
    for i in range(6):
        msgs += [_assistant(f"thinking about step {i}", tool_name=None)]
    in_c = count_chars(msgs)
    out, stats = compact_messages(msgs, max_chars=60, min_tail_steps=2)
    assert stats["compressed"] is True
    assert stats["dropped_steps"] > 0
    assert stats["out_chars"] < in_c
    assert _role(out[1]) == "user"  # prelude 保留


def test_large_observations_get_digest():
    # 大观察（真实场景）：丢弃内容远大于摘要，应插入摘要、明显降 token
    msgs = _make_steps(15, obs_len=500, tool="read_file")
    in_c = count_chars(msgs)
    out, stats = compact_messages(msgs, max_chars=2500, min_tail_steps=3)
    assert stats["compressed"] is True
    assert stats["dropped_steps"] >= 3
    assert stats["out_chars"] < in_c
    # 摘要（user）之后紧跟 assistant——配对完好
    assert _role(out[2]) == "user"
    assert _role(out[3]) == "assistant"
    assert stats["boundary_is_assistant"] is True


def test_only_prelude_no_steps_returns_as_is():
    msgs = [_system(), _user("TASK only")]
    out, stats = compact_messages(msgs, max_chars=1, min_tail_steps=2)
    assert stats["compressed"] is False
    assert out is msgs


def test_token_budget_has_explicit_source_and_priority():
    msgs = _make_steps(12, obs_len=300)

    def counter(values):
        return count_chars(values)

    out, stats = compact_messages(
        msgs,
        max_chars=10_000,
        min_tail_steps=3,
        token_counter=counter,
        max_tokens=3_000,
        tokenizer_name="test-provider",
        tokenizer_estimated=False,
    )
    assert stats["budget_source"] == "tokens"
    assert stats["tokenizer"] == "test-provider"
    assert stats["token_estimated"] is False
    assert stats["in_tokens"] == count_chars(msgs)
    assert stats["out_tokens"] == count_chars(out)


def test_char_fallback_is_marked_as_estimated():
    msgs = _make_steps(2)
    _, stats = compact_messages(msgs, max_chars=10_000, max_tokens=10)
    assert stats["budget_source"] == "tokens"
    assert stats["tokenizer"] == "chars-div-4"
    assert stats["token_estimated"] is True


def test_over_budget_without_safe_tail_is_explicit():
    msgs = [_system("x" * 100), _user("task"), _assistant("only step")]
    out, stats = compact_messages(msgs, max_chars=10, min_tail_steps=1)
    assert out is msgs
    assert stats["compressed"] is False
    assert stats["status"] == "over_budget_uncompressible"


def test_token_budget_counts_inserted_digest():
    msgs = _make_steps(12, obs_len=400)

    def counter(values):
        return count_chars(values)

    _, stats = compact_messages(
        msgs,
        max_chars=10_000,
        min_tail_steps=3,
        token_counter=counter,
        max_tokens=2_500,
        tokenizer_name="test-provider",
        tokenizer_estimated=False,
    )
    assert stats["compressed"] is True
    assert stats["out_tokens"] <= 2_500 or stats["status"] == "over_budget_uncompressible"
