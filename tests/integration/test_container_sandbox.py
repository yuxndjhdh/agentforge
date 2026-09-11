"""Live Docker/Podman security checks.

These tests are intentionally separate from the local unit suite. They skip
when no daemon is available and run in the Linux CI container job.
"""

from __future__ import annotations

import pytest

from agentforge.container_sandbox import ContainerExecutor
from agentforge.sandbox import Sandbox

pytestmark = pytest.mark.integration


def _executor(tmp_path):
    sandbox = Sandbox(backend="auto", disk_limit_mb=0, timeout=5, max_output_bytes=32_000)
    executor = ContainerExecutor(sandbox)
    if not executor.available:
        pytest.skip("Docker/Podman daemon is unavailable")
    diagnostic = executor.diagnose()
    if not diagnostic.available:
        pytest.skip(diagnostic.error or "container daemon is unavailable")
    return executor


def test_container_runs_non_root_and_only_workspace_is_writable(tmp_path):
    executor = _executor(tmp_path)
    result = executor.execute(
        [
            "python",
            "-c",
            "import os; assert os.geteuid() != 0; open('/workspace/agentforge-test.txt', 'w').write('ok')",
        ],
        cwd=str(tmp_path),
    )
    assert result.returncode == 0, result.stderr
    assert (tmp_path / "agentforge-test.txt").read_text(encoding="utf-8") == "ok"


def test_container_applies_cpu_memory_and_pid_limits(tmp_path):
    executor = _executor(tmp_path)
    probe = """
from pathlib import Path

expected = {
    '/sys/fs/cgroup/cpu.max': '100000 100000',
    '/sys/fs/cgroup/memory.max': '536870912',
    '/sys/fs/cgroup/pids.max': '128',
}
for path, value in expected.items():
    actual = Path(path).read_text(encoding='utf-8').strip()
    assert actual == value, f'{path}: {actual!r}'
print('cgroup limits verified')
"""
    result = executor.execute(["python", "-c", probe], cwd=str(tmp_path))
    assert result.returncode == 0, result.stderr
    assert "cgroup limits verified" in result.stdout


def test_container_root_filesystem_is_read_only_outside_workspace(tmp_path):
    executor = _executor(tmp_path)
    probe = """
from pathlib import Path

for target in ('/tmp/agentforge-probe', '/etc/agentforge-probe', '/agentforge-probe'):
    try:
        Path(target).write_text('must be blocked', encoding='utf-8')
    except OSError:
        continue
    raise AssertionError(f'{target} is writable')
Path('/workspace/agentforge-rootfs-probe.txt').write_text('workspace-ok', encoding='utf-8')
"""
    result = executor.execute(["python", "-c", probe], cwd=str(tmp_path))
    assert result.returncode == 0, result.stderr
    assert (tmp_path / "agentforge-rootfs-probe.txt").read_text(encoding="utf-8") == "workspace-ok"


def test_container_has_no_network_or_secret_environment(tmp_path):
    executor = _executor(tmp_path)
    result = executor.execute(
        [
            "python",
            "-c",
            "import os, socket; assert 'API_KEY' not in os.environ; socket.gethostbyname('example.com')",
        ],
        cwd=str(tmp_path),
        env={"API_KEY": "must-not-enter"},
        timeout=2,
    )
    assert result.returncode != 0


def test_container_timeout_and_output_limit_reclaim_process(tmp_path):
    executor = _executor(tmp_path)
    timeout = executor.execute(
        ["python", "-c", "import time; time.sleep(10)"], cwd=str(tmp_path), timeout=0.2
    )
    assert timeout.timed_out
    output = executor.execute(
        ["python", "-c", "print('x' * 1000000)"], cwd=str(tmp_path), max_output_bytes=1024
    )
    assert output.output_limited
    assert len(output.stdout) <= 1024
