from __future__ import annotations

from types import SimpleNamespace

from agentforge.tokenizer import build_token_counter


def test_character_adapter_is_explicit():
    counter, metadata = build_token_counter("deepseek-v4-flash", "char")
    messages = [SimpleNamespace(content=[{"type": "text", "text": "abcd"}])]
    assert counter(messages) == 1
    assert metadata["source"] == "character-fallback"
    assert metadata["estimated"] is True


def test_tiktoken_adapter_counts_messages_when_available():
    import pytest

    pytest.importorskip("tiktoken")
    counter, metadata = build_token_counter("gpt-4o", "auto")
    messages = [SimpleNamespace(content=[{"type": "text", "text": "hello world"}])]
    assert counter(messages) > 0
    assert metadata["source"] == "tiktoken"
    assert metadata["available"] is True
