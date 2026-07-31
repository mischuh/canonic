"""Tests for ``canonic completion`` — prints a shell completion script."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from canonic.cli.app import app

if TYPE_CHECKING:
    from typer.testing import CliRunner


@pytest.mark.parametrize("shell", ["bash", "zsh", "fish"])
def test_completion_prints_script_for_supported_shell(runner: CliRunner, shell: str) -> None:
    result = runner.invoke(app, ["completion", "--shell", shell])
    assert result.exit_code == 0, result.output
    assert "canonic" in result.output
    assert result.output.strip()


def test_completion_zsh_script_is_compdef(runner: CliRunner) -> None:
    result = runner.invoke(app, ["completion", "--shell", "zsh"])
    assert "compdef" in result.output


def test_completion_unsupported_shell_errors(runner: CliRunner) -> None:
    result = runner.invoke(app, ["completion", "--shell", "cmd"])
    assert result.exit_code != 0
    assert "not supported" in result.output


def test_completion_needs_no_project(runner: CliRunner, outside_project: None) -> None:
    """Unlike query/sql/mcp, completion has nothing to do with a canonic project."""
    result = runner.invoke(app, ["completion", "--shell", "bash"])
    assert result.exit_code == 0, result.output


def test_completion_auto_detect_falls_back_cleanly(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When shell detection fails and --shell is omitted, fail with a clear message."""
    import shellingham

    def _raise(*args: object, **kwargs: object) -> None:
        raise shellingham.ShellDetectionFailure

    monkeypatch.setattr(shellingham, "detect_shell", _raise)
    result = runner.invoke(app, ["completion"])
    assert result.exit_code != 0
    assert "could not detect your shell" in result.output
