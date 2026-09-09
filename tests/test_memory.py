"""分层记忆层单测（无需 LLM）：MemoryStore 读写/搜索/持久化 + memory_instructions 注入。"""

from __future__ import annotations

import glob

from agentforge.memory import MemoryStore, memory_instructions
from agentforge.tools import all_code_tools


def _store(wd) -> MemoryStore:
    return MemoryStore(str(wd))


def test_save_then_search_roundtrip(tmp_path):
    s = _store(tmp_path)
    s.save("durable", "calc-add", "add returns a + b")
    assert "calc-add: add returns a + b" in s.search("a + b")
    assert "calc-add" in s.search("CALC")  # 大小写不敏感
    assert "(无匹配记忆)" in s.search("zzz-no-such")


def test_daily_separate_file_and_searchable(tmp_path):
    s = _store(tmp_path)
    s.save("durable", "k", "durable content")
    s.save("daily", "note", "today fixed the bug")
    assert s.search("fixed the bug")
    files = [
        f.replace("\\", "/") for f in glob.glob(str(tmp_path / ".agentforge" / "memory" / "*"))
    ]
    days = [f for f in files if "daily-" in f]
    dur = [f for f in files if f.endswith("durable.json")]
    assert days and dur  # 分层文件各自存在


def test_tier_validation(tmp_path):
    s = _store(tmp_path)
    assert s.save("ephemeral", "k", "v").startswith("Error: 未知 tier")
    assert s.save("durable", "", "v").startswith("Error: key")


def test_persists_across_store_instances(tmp_path):
    _store(tmp_path).save("durable", "k", "hello world")
    fresh = _store(tmp_path)  # 新实例，模拟跨会话
    assert "hello world" in fresh.search("hello")


def test_memory_instructions_injects_durable_only(tmp_path):
    s = _store(tmp_path)
    assert memory_instructions(str(tmp_path)) == ""  # 无 durable 时为空
    s.save("durable", "k", "remember this")
    text = memory_instructions(str(tmp_path))
    assert "[持久记忆 (durable)]" in text
    assert "remember this" in text
    s.save("daily", "d", "daily thing")
    # daily 不进 instructions（只有 durable 注入系统提示）
    assert "daily thing" not in memory_instructions(str(tmp_path))


def test_memory_tools_forward(tmp_path):
    tools = {t.name: t for t in all_code_tools(str(tmp_path))}
    assert "memory_save" in tools and "memory_search" in tools
    tools["memory_save"].forward("durable", "x", "alpha beta")
    out = tools["memory_search"].forward("beta")
    assert "[durable]" in out and "x: alpha beta" in out
