"""Tests for the root CLI app: help tree, version, bare invocation, and stubs."""

from importlib.metadata import version

import pytest
from typer.testing import CliRunner

from canon.cli.app import app

_GROUPS = ["setup", "connection", "sl", "query", "sql", "knowledge", "status", "mcp", "completion"]

# Capability stubs that must exit 0 with a "not implemented yet" notice.
_STUBS = [
    ["setup"],
    ["connection", "add"],
    ["connection", "test"],
    ["connection", "list"],
    ["connection", "remove"],
    ["sl", "resolve", "revenue"],
    ["sl", "compile", "-f", "q.json"],
    ["query", "-f", "q.json"],
    ["sql", "SELECT 1"],
    ["knowledge", "search", "orders"],
    ["completion"],
]

# MCP commands are now real (E8) — they require a project directory and exit non-zero
# when run outside one. Excluded from the generic stub test above.
_MCP_COMMANDS = [
    ["mcp", "start"],
    ["mcp", "stop"],
    ["mcp", "status"],
]


def test_help_lists_all_subcommand_groups(runner: CliRunner) -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    for group in _GROUPS:
        assert group in result.output


def test_version_prints_package_version(runner: CliRunner) -> None:
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert result.output.strip() == version("canon")


def test_bare_invocation_exits_zero_without_traceback(runner: CliRunner) -> None:
    result = runner.invoke(app, [])
    assert result.exit_code == 0
    assert "not implemented yet" in result.output
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
def test_mcp_commands_require_project(runner: CliRunner, argv: list[str]) -> None:
    """MCP commands are real (E8) and exit non-zero outside a project directory."""
    result = runner.invoke(app, argv)
    assert result.exit_code != 0
    assert "no canon project found" in result.output
