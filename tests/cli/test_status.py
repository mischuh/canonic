"""Tests for ``canon status``."""

import json
from pathlib import Path
from typing import Any

from typer.testing import CliRunner

from canon.cli.app import app

_BASE_EVENT: dict[str, Any] = {
    "ts": "2026-01-01T00:00:00+00:00",
    "kind": "served_answer",
    "contract_schema": "1.5",
    "query_hash": "sha256:aaa",
    "compiled_sql_hash": "sha256:bbb",
    "connection": "wh",
    "resolved": {},
    "guardrails_fired": [],
    "finality": None,
    "freshness": [],
    "latency_ms": 100,
    "bytes_scanned": None,
    "error": None,
    "trust_score": None,
    "cache_hit": None,
    "over_limit_blocked": None,
}


def _write_events(dotcanon: Path, events: list[dict[str, Any]]) -> None:
    dotcanon.mkdir(parents=True, exist_ok=True)
    (dotcanon / "events.jsonl").write_text(
        "\n".join(json.dumps(e, sort_keys=True) for e in events) + "\n"
    )


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
    assert "1.5" in result.output  # contract_schema


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
    assert payload["contract_schema"] == "1.5"


def test_status_reports_invalid_config(runner: CliRunner, tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "canon.yaml").write_text("version: 1\nproject:\n  name: x\n")  # missing llm
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["status"])
    assert result.exit_code == 0
    assert str(tmp_path) in result.output
    assert "invalid" in result.output


def test_status_shows_event_summary(runner: CliRunner, project_dir: Path) -> None:
    _write_events(
        project_dir / ".canon",
        [
            {**_BASE_EVENT, "error": None, "latency_ms": 50},
            {**_BASE_EVENT, "error": "unresolved", "latency_ms": 200},
        ],
    )
    result = runner.invoke(app, ["status"])
    assert result.exit_code == 0
    assert "2 served" in result.output
    assert "errors 1" in result.output


def test_status_no_event_summary_when_empty(runner: CliRunner, project_dir: Path) -> None:
    result = runner.invoke(app, ["status"])
    assert result.exit_code == 0
    assert "served" not in result.output


def test_status_json_includes_events(runner: CliRunner, project_dir: Path) -> None:
    _write_events(
        project_dir / ".canon",
        [{**_BASE_EVENT, "error": None, "latency_ms": 75}],
    )
    result = runner.invoke(app, ["--json", "status"])
    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["events"]["count"] == 1
    assert payload["events"]["error_count"] == 0
    assert payload["events"]["latency_p95_ms"] == 75


def test_status_json_events_zero_when_no_log(runner: CliRunner, project_dir: Path) -> None:
    result = runner.invoke(app, ["--json", "status"])
    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["events"]["count"] == 0
    assert payload["events"]["latency_p95_ms"] is None
