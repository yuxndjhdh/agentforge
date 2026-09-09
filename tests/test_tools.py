"""代码工具单测（无需 LLM）。"""

from __future__ import annotations

from agentforge.tools import GrepTool, ListDirTool, ReadFileTool, RunCommandTool, WriteFileTool


def test_read_write_roundtrip(tmp_path):
    wd = str(tmp_path)
    (tmp_path / "a.txt").write_text("hello agentforge\nsecond line\n", encoding="utf-8")
    out = ReadFileTool(wd).forward("a.txt")
    assert "hello agentforge" in out
    WriteFileTool(wd).forward("b.txt", "written content")
    assert (tmp_path / "b.txt").read_text(encoding="utf-8") == "written content"


def test_write_creates_parents(tmp_path):
    wd = str(tmp_path)
    WriteFileTool(wd).forward("sub/deep/c.txt", "x")
    assert (tmp_path / "sub" / "deep" / "c.txt").exists()


def test_path_escape_blocked(tmp_path):
    wd = str(tmp_path)
    r = ReadFileTool(wd).forward("../outside.txt")
    assert "escape" in r.lower()
    r2 = WriteFileTool(wd).forward("../../etc/passwd", "boom")
    assert "escape" in r2.lower()


def test_missing_file(tmp_path):
    out = ReadFileTool(str(tmp_path)).forward("nope.txt")
    assert "no such file" in out


def test_list_and_grep(tmp_path):
    wd = str(tmp_path)
    (tmp_path / "x.py").write_text("def foo():\n    pass\n# TODO fix\n", encoding="utf-8")
    assert "x.py" in ListDirTool(wd).forward(".")
    g = GrepTool(wd).forward("def |TODO", ".")
    assert "x.py:1: def foo" in g
    g2 = GrepTool(wd).forward("nomatch_xyz", ".")
    assert "no matches" in g2


def test_grep_bad_pattern(tmp_path):
    out = GrepTool(str(tmp_path)).forward("(", ".")
    assert "bad pattern" in out


def test_run_command_exitcode(tmp_path):
    out = RunCommandTool(str(tmp_path)).forward("python --version")
    assert "exit=" in out
