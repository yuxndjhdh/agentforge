from __future__ import annotations

import pytest

from agentforge import cli


def test_cli_help_exits_cleanly_without_running_a_command(capsys):
    with pytest.raises(SystemExit) as exit_info:
        cli.main(["--help"])
    assert exit_info.value.code == 0
    assert "agentforge" in capsys.readouterr().out


def test_cli_rejects_unknown_subcommand(capsys):
    with pytest.raises(SystemExit) as exit_info:
        cli.main(["definitely-not-a-subcommand"])
    assert exit_info.value.code != 0
    assert "definitely-not-a-subcommand" in capsys.readouterr().err


def test_cli_reports_unknown_verify_task_as_usage_error(capsys):
    assert cli.main(["verify", "no-such-task"]) == 2
    assert "no-such-task" in capsys.readouterr().err
