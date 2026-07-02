"""Tests for the root CLI app: help tree, version, bare invocation, and stubs."""

from importlib.metadata import version

import pytest
from typer.testing import CliRunner

from canonic.cli.app import app

_GROUPS = ["setup", "connection", "sl", "query", "sql", "knowledge", "status", "mcp", "completion"]

# Capability stubs that must exit 0 with a "not implemented yet" notice.
# (``setup`` is now a real interactive command — see tests/cli/test_setup.py.)
_STUBS = [
    ["knowledge", "search", "orders"],
    ["completion"],
]

# Real capability commands (E5/E7/E8/E2) that require a project directory and exit
# non-zero when run outside one. Excluded from the stub test.
_MCP_COMMANDS = [
    ["mcp", "start"],
    ["mcp", "stop"],
    ["mcp", "status"],
    ["sql", "SELECT 1"],
    ["sl", "resolve", "revenue"],
]


def test_help_lists_all_subcommand_groups(runner: CliRunner) -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    for group in _GROUPS:
        assert group in result.output


def test_version_prints_package_version(runner: CliRunner) -> None:
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert result.output.strip() == version("canonic")


def test_bare_invocation_shows_project_menu(runner: CliRunner, project_dir: None) -> None:
    result = runner.invoke(app, [], input="4\n")
    assert result.exit_code == 0, result.output
    assert result.exception is None or isinstance(result.exception, SystemExit)


def test_bare_invocation_json_exits_2(runner: CliRunner) -> None:
    result = runner.invoke(app, ["--json"])
    assert result.exit_code == 2
    assert result.exception is None or isinstance(result.exception, SystemExit)


@pytest.mark.parametrize("argv", _STUBS, ids=lambda a: " ".join(a))
def test_stub_commands_exit_zero(runner: CliRunner, argv: list[str]) -> None:
    result = runner.invoke(app, argv)
    assert result.exit_code == 0, result.output
    assert "not implemented yet" in result.output


@pytest.mark.parametrize("argv", _STUBS, ids=lambda a: " ".join(a))
def test_stub_commands_json_mode(runner: CliRunner, argv: list[str]) -> None:
    result = runner.invoke(app, ["--json", *argv])
    assert result.exit_code == 0, result.output
    assert '"status": "not_implemented"' in result.output


@pytest.mark.parametrize("argv", _MCP_COMMANDS, ids=lambda a: " ".join(a))
def test_mcp_commands_require_project(
    runner: CliRunner, outside_project: None, argv: list[str]
) -> None:
    """MCP/sql commands are real and exit non-zero outside a project directory."""
    result = runner.invoke(app, argv)
    assert result.exit_code != 0
    assert "no canonic project found" in result.output


def test_query_missing_file_is_clean_error(runner: CliRunner) -> None:
    """A missing query file is a typer validation error, not a traceback."""
    result = runner.invoke(app, ["query", "-f", "does-not-exist.json"])
    assert result.exit_code != 0
    assert result.exception is None or isinstance(result.exception, SystemExit)


def test_sl_compile_missing_file_is_clean_error(runner: CliRunner) -> None:
    """A missing compile query file is a typer validation error, not a traceback."""
    result = runner.invoke(app, ["sl", "compile", "-f", "does-not-exist.json"])
    assert result.exit_code != 0
    assert result.exception is None or isinstance(result.exception, SystemExit)
