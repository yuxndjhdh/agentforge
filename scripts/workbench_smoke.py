"""Exercise the workbench page and API lifecycle without an external model."""

from __future__ import annotations

import json
import shlex
import subprocess
import sys
import tempfile
import time
from html.parser import HTMLParser
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient

from agentforge.api import create_app
from agentforge.api.app import RunService
from agentforge.config import ModelConfig


class _PageParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.ids: set[str] = set()

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        del tag
        attributes = dict(attrs)
        value = attributes.get("id")
        if value:
            self.ids.add(value)


class _FakeAgent:
    def __init__(self, workdir: str) -> None:
        self.workdir = Path(workdir)
        self.memory = type("Memory", (), {"steps": []})()
        self.model = type("Model", (), {"compressions": []})()
        self.tools = {}

    def run(self, _task: str, reset: bool = True, max_steps: int = 20) -> str:
        del reset, max_steps
        target = self.workdir / "README.md"
        target.write_text(target.read_text(encoding="utf-8") + "smoke complete\n", encoding="utf-8")
        return "workbench smoke completed"


def _run_git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )


def _assert(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def _wait_for_terminal(client: TestClient, run_id: str) -> dict:
    deadline = time.monotonic() + 5.0
    latest: dict = {}
    while time.monotonic() < deadline:
        response = client.get(f"/runs/{run_id}")
        _assert(response.status_code == 200, f"run lookup failed: {response.status_code}")
        latest = response.json()
        if latest["status"] in {"succeeded", "failed", "cancelled"}:
            return latest
        time.sleep(0.02)
    raise AssertionError(f"run did not finish: {latest}")


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="agentforge-workbench-") as directory:
        root = Path(directory)
        repo = root / "repo"
        repo.mkdir()
        (repo / "README.md").write_text("# smoke\n", encoding="utf-8")
        _run_git(repo, "init")
        _run_git(repo, "config", "user.name", "AgentForge Smoke")
        _run_git(repo, "config", "user.email", "agentforge-smoke@example.invalid")
        _run_git(repo, "add", "README.md")
        _run_git(repo, "-c", "commit.gpgsign=false", "commit", "-m", "initial")

        cfg = ModelConfig(
            base_url="http://fake",
            api_key="",
            model="smoke-model",
            sandbox_backend="local",
            sandbox_disk_limit_mb=0,
            state_db=str(root / "state.sqlite3"),
            trace_dir=str(root / "traces"),
        )

        def fake_make_agent(_cfg, workdir: str, **_kwargs):
            return _FakeAgent(workdir)

        with patch("agentforge.harness.make_agent", side_effect=fake_make_agent):
            service = RunService(cfg)
            app = create_app(service=service)
            with TestClient(app) as client:
                page = client.get("/")
                _assert(page.status_code == 200, f"workbench page failed: {page.status_code}")
                parser = _PageParser()
                parser.feed(page.text)
                required_ids = {
                    "run-form", "repo", "task", "model", "sandbox-backend", "max-steps",
                    "max-duration", "verify-command", "verify-attempts", "start", "run-panel",
                    "run-id", "run-status", "cancel", "resume", "export", "overview", "diff",
                    "attempts", "tool-calls", "metrics", "controls", "events",
                }
                _assert(required_ids <= parser.ids, f"workbench is missing DOM ids: {sorted(required_ids - parser.ids)}")
                for endpoint in ("/config", "/runs", "/trace", "/export", "/cancel", "/resume"):
                    _assert(endpoint in page.text, f"workbench does not reference {endpoint}")

                verify_code = (
                    "from pathlib import Path; "
                    "raise SystemExit(0 if Path('README.md').read_text(encoding='utf-8').endswith('smoke complete\\n') else 1)"
                )
                verify_command = f"{shlex.quote(sys.executable)} -c {shlex.quote(verify_code)}"
                created = client.post(
                    "/runs",
                    json={
                        "repo": str(repo),
                        "task": "update the README",
                        "run_id": "workbench-smoke",
                        "model": "smoke-request-model",
                        "sandbox_backend": "local",
                        "max_steps": 5,
                        "verify_command": verify_command,
                        "verify_attempts": 1,
                    },
                )
                _assert(created.status_code == 202, f"run submission failed: {created.text}")

                final = _wait_for_terminal(client, "workbench-smoke")
                _assert(final["status"] == "succeeded", f"workbench run failed: {final}")

                detail_response = client.get("/runs/workbench-smoke/trace")
                _assert(detail_response.status_code == 200, "trace endpoint failed")
                detail = detail_response.json()
                _assert(detail["config"]["model"] == "smoke-request-model", "request model was not persisted")
                _assert(detail["config"]["sandbox_backend"] == "local", "sandbox backend was not persisted")
                _assert(detail["metrics"]["verification_count"] == 1, "verification was not recorded")
                _assert(detail["metrics"]["verification_passed"] == 1, "verification did not pass")
                _assert(detail["checkpoint"]["exists"], "checkpoint was not recorded")
                _assert(detail["diff"]["available"], "workspace diff was unavailable")
                _assert("README.md" in detail["diff"]["files"], "workspace diff omitted README.md")
                _assert("smoke complete" in detail["diff"]["text"], "workspace diff omitted the change")

                summary = client.get("/runs/workbench-smoke/summary")
                _assert(summary.status_code == 200, "summary endpoint failed")
                _assert(summary.json()["metrics"] == detail["metrics"], "summary and trace metrics differ")
                exported = client.get("/runs/workbench-smoke/export")
                _assert(exported.status_code == 200, "export endpoint failed")
                _assert("attachment" in exported.headers.get("content-disposition", ""), "export is not downloadable")
                _assert(json.loads(exported.text)["run"]["id"] == "workbench-smoke", "export has the wrong run")

    print("workbench smoke: passed")
    print("page: served and required workbench controls were present")
    print("flow: submit -> verify -> trace -> summary -> export -> diff")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
