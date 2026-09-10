"""Pluggable message token counting for context budgets.

The preferred adapter uses ``tiktoken`` when installed. Unknown providers are
marked as estimated because an OpenAI-compatible endpoint may use a different
tokenizer; the deterministic character fallback is explicit in trace metadata.
"""

from __future__ import annotations

from typing import Any, Callable

TokenCounter = Callable[[list[Any]], int]


def _message_text(message: Any) -> str:
    content = getattr(message, "content", None)
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    parts: list[str] = []
    for item in content:
        if isinstance(item, dict):
            parts.append(str(item.get("text", "")))
        else:
            parts.append(str(item))
    return "".join(parts)


def _character_counter(messages: list[Any]) -> int:
    return max(1, sum(len(_message_text(message)) for message in messages) // 4)


def build_token_counter(
    model: str,
    requested: str = "auto",
) -> tuple[TokenCounter, dict[str, Any]]:
    """Build a counter and return traceable adapter metadata.

    ``requested=char`` deliberately selects the deterministic fallback. With
    ``auto``, known OpenAI model names use ``encoding_for_model`` and other
    names use the explicit ``cl100k_base`` compatibility adapter.
    """
    choice = str(requested or "auto").strip().lower()
    if choice in {"char", "chars", "character", "estimate"}:
        return _character_counter, {
            "name": "chars-div-4",
            "source": "character-fallback",
            "estimated": True,
            "available": True,
        }

    try:
        import tiktoken
    except ImportError:
        return _character_counter, {
            "name": "chars-div-4",
            "source": "character-fallback",
            "estimated": True,
            "available": False,
            "reason": "tiktoken is not installed",
        }

    encoding = None
    encoding_name = choice if choice not in {"", "auto"} else "cl100k_base"
    exact = False
    if choice in {"", "auto"}:
        try:
            encoding = tiktoken.encoding_for_model(model)
            encoding_name = encoding.name
            exact = model.lower().startswith(("gpt-", "text-"))
        except KeyError:
            encoding = tiktoken.get_encoding("cl100k_base")
    else:
        encoding = tiktoken.get_encoding(encoding_name)

    def counter(messages: list[Any]) -> int:
        return max(
            1,
            sum(
                len(encoding.encode(_message_text(message), disallowed_special=()))
                for message in messages
            ),
        )

    return counter, {
        "name": f"tiktoken:{encoding_name}",
        "source": "tiktoken",
        "estimated": not exact,
        "available": True,
    }
