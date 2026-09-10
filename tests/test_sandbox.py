"""工具级沙箱单测（无需 LLM）：check_command / check_write / env 消杀 / 配置解析 + 工具透传。"""

from __future__ import annotations

import os
import shutil
import sys
import time

import pytest

from agentforge.config import ModelConfig
from agentforge.container_sandbox import ContainerExecutor
from agentforge.paths import resolve_path
from agentforge.sandbox import DEFAULT_DENY_COMMANDS, DEFAULT_DENY_PATTERNS, Sandbox
from agentforge.tools import ReadFileTool, WriteFileTool, all_code_tools


def test_deny_rm_rf_but_allow_python():
    s = Sandbox()
    assert "denied" in s.check_command("rm -rf .")
    assert s.check_command("python --version") is None


def test_deny_curl_and_wget_by_command_name():
    s = Sandbox()
    assert "denied" in s.check_command("curl http://x")
    assert "denied" in s.check_command("wget http://x")


def test_deny_pattern_catches_curl_inline():
    s = Sandbox()
    assert "matches sandbox deny pattern" in s.check_command("python -c 'import os; os.system(\"curl\")'")


def test_whitelist_gating():
    s = Sandbox(whitelist=("python", "ls"))
    assert "not in sandbox whitelist" in s.check_command("cat x")
    assert s.check_command("ls") is None
    assert s.check_command("python --version") is None


def test_readonly_blocks_write_and_run():
    s = Sandbox(readonly=True)
    assert "readonly" in s.check_write()
    # readonly 挡的是"写/跑"（工具层先 check_write），check_command 本身只看命令
    assert s.check_command("python --version") is None


def test_command_tool_deny_pattern_via_forward():
    tools = {t.name: t for t in all_code_tools(str("."), Sandbox())}
    assert "Error" in tools["run_command"].forward("curl http://x")
    assert "exit=" in tools["run_command"].forward("python --version")


def test_readonly_tool_forward_blocks_write():
    import os
    import tempfile

    wd = tempfile.mkdtemp(prefix="agentforge-sb-")
    with open(os.path.join(wd, "a.txt"), "w", encoding="utf-8") as f:
        f.write("hi")
    tools = {t.name: t for t in all_code_tools(wd, Sandbox(readonly=True))}
    assert "Error" in tools["write_file"].forward("a.txt", "hi")
    assert "Error" in tools["run_command"].forward("python --version")
    # 只读不影响读
    assert "Error" not in tools["read_file"].forward("a.txt")


def test_env_scrub_removes_secrets():
    base = {"HARNESS_LLM_KEY": "sk-x", "OPENAI_API_KEY": "sk-y", "PATH": "/usr/bin", "safe": "1"}
    env = Sandbox().env(base=base)
    assert "sk-x" not in str(env.values())
    assert "sk-y" not in str(env.values())
    assert env["AGENTFORGE_SANDBOXED"] == "1"
    assert env["safe"] == "1"


def test_inherits_real_os_environ_without_key():
    import os
    os.environ["HARNESS_LLM_KEY"] = "sk-secret"
    env = Sandbox().env()
    assert "sk-secret" not in str(env.values())
    assert env["AGENTFORGE_SANDBOXED"] == "1"


def test_config_parsing_defaults_and_overrides():
    cfg = ModelConfig(
        base_url="x", api_key="k", model="m",
        sandbox_whitelist="python, ls",
        sandbox_denylist="make",
        sandbox_deny_patterns=r"\breset\b",
        sandbox_readonly=True,
        sandbox_timeout=5.0,
    )
    s = Sandbox.from_config(cfg)
    assert s.whitelist == ("python", "ls")
    assert "make" in s.deny_commands and "rm" in s.deny_commands
    assert any("reset" in p for p in s.deny_patterns)
    assert len(s.deny_patterns) > len(DEFAULT_DENY_PATTERNS)
    assert len(s.deny_commands) > len(DEFAULT_DENY_COMMANDS)
    assert s.readonly is True
    assert s.timeout == 5.0


def test_config_parsing_all_empty_uses_defaults():
    cfg = ModelConfig(base_url="x", api_key="k", model="m")
    s = Sandbox.from_config(cfg)
    assert s.whitelist == ()
    assert s.deny_commands == DEFAULT_DENY_COMMANDS
    assert s.deny_patterns == DEFAULT_DENY_PATTERNS
    assert s.readonly is False


def test_mutating_tools_use_passed_sandbox():
    wd = "."
    sb = Sandbox(readonly=True)
    tools = all_code_tools(wd, sb)
    for t in tools:
        if t.name in ("write_file", "run_command", "memory_save"):
            assert t.sandbox is sb
        else:
            assert t.sandbox is not sb


def test_path_boundary_rejects_symlink_and_hardlink(tmp_path):
    outside = tmp_path.parent / "agentforge-outside.txt"
    outside.write_text("outside", encoding="utf-8")
    link = tmp_path / "linked.txt"
    try:
        os.link(outside, link)
    except OSError as exc:
        pytest.skip(f"hard links unavailable: {exc}")
    assert resolve_path(str(tmp_path), "linked.txt", allow_missing=False, reject_hardlink=True).path is None
    assert "Error" in ReadFileTool(str(tmp_path)).forward("linked.txt")
    assert "Error" in WriteFileTool(str(tmp_path)).forward("linked.txt", "overwrite")

    symlink = tmp_path / "symlink.txt"
    try:
        symlink.symlink_to(outside)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"symlinks unavailable: {exc}")
    assert resolve_path(str(tmp_path), "symlink.txt", allow_missing=False).path is None
    assert "Error" in ReadFileTool(str(tmp_path)).forward("symlink.txt")


def test_local_executor_stops_on_timeout_and_output_limit(tmp_path):
    sandbox = Sandbox(backend="local", timeout=0.2, max_output_bytes=1024)
    started = time.monotonic()
    timed_out = sandbox.execute([sys.executable, "-c", "import time; time.sleep(2)"], cwd=str(tmp_path))
    assert timed_out.timed_out
    assert time.monotonic() - started < 1.5

    output = sandbox.execute(
        [sys.executable, "-c", "print('x' * 1000000)"], cwd=str(tmp_path), timeout=5
    )
    assert output.output_limited
    assert len(output.stdout) <= 1024


def test_container_diagnostics_and_explicit_backend_fail_closed(monkeypatch, tmp_path):
    monkeypatch.setattr(shutil, "which", lambda _name: None)
    sandbox = Sandbox(backend="auto")
    diagnostic = ContainerExecutor(sandbox).diagnose().to_dict()
    assert diagnostic["available"] is False
    assert diagnostic["storage_limit_status"] == "unavailable"
    assert diagnostic["image_pinned"] is True

    result = Sandbox(backend="docker").execute([sys.executable, "-c", "print('no')"], cwd=str(tmp_path))
    assert result.returncode is None
    assert "unavailable" in (result.error or "")


def test_auto_container_fallback_is_recorded(monkeypatch, tmp_path, caplog):
    monkeypatch.setattr(shutil, "which", lambda _name: None)
    events = []
    sandbox = Sandbox(backend="auto", decision_sink=events.append)
    with caplog.at_level("WARNING"):
        result = sandbox.execute([sys.executable, "-c", "print('local')"], cwd=str(tmp_path))
    assert result.returncode == 0
    assert "local" in result.stdout
    assert any(event["action"] == "backend_fallback" for event in events)
    assert "falling back to local" in caplog.text
