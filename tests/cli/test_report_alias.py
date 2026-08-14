"""Tests for the deprecated ``canonic report`` alias of ``canonic audit``."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from canonic.cli.app import app

if TYPE_CHECKING:
    from pathlib import Path

    from typer.testing import CliRunner

_DEPRECATION_NOTICE = '"canonic report" is deprecated, use "canonic audit"'


def test_report_alias_prints_deprecation_notice_to_stderr(
    runner: CliRunner, project_dir: Path
) -> None:
    result = runner.invoke(app, ["report"])
    assert result.exit_code == 0
    assert _DEPRECATION_NOTICE in result.stderr


def test_report_alias_matches_audit_stdout(runner: CliRunner, project_dir: Path) -> None:
    audit_result = runner.invoke(app, ["audit"])
    report_result = runner.invoke(app, ["report"])
    assert report_result.exit_code == audit_result.exit_code
    assert report_result.stdout == audit_result.stdout


def test_report_alias_json_output_unaffected_by_deprecation_notice(
    runner: CliRunner, project_dir: Path
) -> None:
    result = runner.invoke(app, ["--json", "report"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["count"] == 0
    assert _DEPRECATION_NOTICE in result.stderr


def test_report_alias_help_notes_deprecation(runner: CliRunner) -> None:
    result = runner.invoke(app, ["report", "--help"])
    assert result.exit_code == 0
    assert "deprecated" in result.output.lower()
