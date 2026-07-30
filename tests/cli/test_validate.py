"""Tests for ``canonic validate``."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from canonic.cli.app import app

if TYPE_CHECKING:
    from pathlib import Path

    from typer.testing import CliRunner


def test_validate_outside_project(runner: CliRunner, outside_project) -> None:
    result = runner.invoke(app, ["validate"])
    assert result.exit_code == 1
    assert "no canonic project found" in result.output


def test_validate_inside_empty_project_ok(runner: CliRunner, project_dir: Path) -> None:
    result = runner.invoke(app, ["validate"])
    assert result.exit_code == 0
    assert "ok" in result.output
    assert str(project_dir) in result.output


def test_validate_json_output(runner: CliRunner, project_dir: Path) -> None:
    result = runner.invoke(app, ["--json", "validate"])
    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload == {"status": "ok", "project_root": str(project_dir)}


def test_validate_reports_contract_error(runner: CliRunner, project_dir: Path) -> None:
    """A binding whose canonical.source does not exist fails with a structured error."""
    (project_dir / "semantics" / "db").mkdir(parents=True)
    (project_dir / "contracts" / "metrics").mkdir(parents=True)

    (project_dir / "semantics" / "db" / "src.yaml").write_text(
        "name: src\nconnection: db\ntable: src\n"
        "grain: [id]\n"
        "columns:\n"
        "  - {name: id, type: string, nullable: false}\n"
        "  - {name: val, type: decimal, nullable: true}\n"
        "measures:\n"
        "  - {name: total, expr: 'sum(val)', additivity: additive}\n"
    )
    (project_dir / "contracts" / "metrics" / "m.yaml").write_text(
        "metric: broken_metric\ncanonical:\n"
        "  source: does_not_exist\n"
        "  measure: total\n"
        "status: active\n"
    )

    result = runner.invoke(app, ["validate"])
    assert result.exit_code != 0
    assert "does_not_exist" in result.output


def test_validate_json_reports_contract_error(runner: CliRunner, project_dir: Path) -> None:
    (project_dir / "semantics" / "db").mkdir(parents=True)
    (project_dir / "contracts" / "metrics").mkdir(parents=True)

    (project_dir / "semantics" / "db" / "src.yaml").write_text(
        "name: src\nconnection: db\ntable: src\n"
        "grain: [id]\n"
        "columns:\n"
        "  - {name: id, type: string, nullable: false}\n"
        "  - {name: val, type: decimal, nullable: true}\n"
        "measures:\n"
        "  - {name: total, expr: 'sum(val)', additivity: additive}\n"
    )
    (project_dir / "contracts" / "metrics" / "m.yaml").write_text(
        "metric: broken_metric\ncanonical:\n"
        "  source: does_not_exist\n"
        "  measure: total\n"
        "status: active\n"
    )

    result = runner.invoke(app, ["--json", "validate"])
    assert result.exit_code != 0
    payload = json.loads(result.output)
    assert "does_not_exist" in payload["message"]
