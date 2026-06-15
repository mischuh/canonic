"""Tests for ``canon status``."""

import json
from pathlib import Path

from typer.testing import CliRunner

from canon.cli.app import app


def test_status_outside_project(runner: CliRunner, tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["status"])
    assert result.exit_code == 0
    assert "no canon project found" in result.output


def test_status_inside_project_prints_root_and_version(
    runner: CliRunner, project_dir: Path
) -> None:
    result = runner.invoke(app, ["status"])
    assert result.exit_code == 0
    assert str(project_dir) in result.output
    assert "config version: 1" in result.output
    assert "absent" in result.output  # no .canon/ yet
    assert "1.0" in result.output  # contract_schema


def test_status_detects_dotcanon(runner: CliRunner, project_dir: Path) -> None:
    (project_dir / ".canon").mkdir()
    result = runner.invoke(app, ["status"])
    assert "present" in result.output


def test_status_json_outside_project(runner: CliRunner, tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["--json", "status"])
    assert result.exit_code == 0
    assert json.loads(result.output) == {"project_root": None}


def test_status_json_inside_project(runner: CliRunner, project_dir: Path) -> None:
    result = runner.invoke(app, ["--json", "status"])
    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["project_root"] == str(project_dir)
    assert payload["config_version"] == 1
    assert payload["dotcanon_present"] is False
    assert payload["config_error"] is None
    assert payload["contract_schema"] == "1.0"


def test_status_reports_invalid_config(runner: CliRunner, tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "canon.yaml").write_text("version: 1\nproject:\n  name: x\n")  # missing llm
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["status"])
    assert result.exit_code == 0
    assert str(tmp_path) in result.output
    assert "invalid" in result.output
