"""Tests for ``canon report``."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from canon.cli.app import app

_BASE_EVENT: dict[str, Any] = {
    "ts": "2026-01-01T00:00:00+00:00",
    "kind": "served_answer",
    "contract_schema": "1.1",
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


def _event(**overrides: Any) -> dict[str, Any]:
    return {**_BASE_EVENT, **overrides}


def _write_events(dotcanon: Path, events: list[dict[str, Any]]) -> None:
    dotcanon.mkdir(parents=True, exist_ok=True)
    (dotcanon / "events.jsonl").write_text(
        "\n".join(json.dumps(e, sort_keys=True) for e in events) + "\n"
    )


# ---------------------------------------------------------------------------
# Outside project
# ---------------------------------------------------------------------------


def test_report_outside_project(runner: CliRunner, tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["report"])
    assert result.exit_code == 0
    assert "no canon project found" in result.output


def test_report_outside_project_json(runner: CliRunner, tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["--json", "report"])
    assert result.exit_code == 0
    assert json.loads(result.output) == {"project_root": None}


# ---------------------------------------------------------------------------
# Empty log
# ---------------------------------------------------------------------------


def test_report_empty_log(runner: CliRunner, project_dir: Path) -> None:
    result = runner.invoke(app, ["report"])
    assert result.exit_code == 0
    assert "no served answers recorded yet" in result.output


def test_report_empty_log_json(runner: CliRunner, project_dir: Path) -> None:
    result = runner.invoke(app, ["--json", "report"])
    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["count"] == 0
    assert payload["error_distribution"] == {}
    assert payload["latency"] is None
    assert payload["bytes_scanned"] is None
    assert payload["telemetry_enabled"] is False


# ---------------------------------------------------------------------------
# Populated log
# ---------------------------------------------------------------------------


def test_report_shows_counts(runner: CliRunner, project_dir: Path) -> None:
    _write_events(
        project_dir / ".canon",
        [_event(latency_ms=100), _event(latency_ms=200, error="unresolved")],
    )
    result = runner.invoke(app, ["report"])
    assert result.exit_code == 0
    assert "2" in result.output


def test_report_shows_error_distribution(runner: CliRunner, project_dir: Path) -> None:
    _write_events(
        project_dir / ".canon",
        [_event(), _event(error="unresolved"), _event(error="unresolved")],
    )
    result = runner.invoke(app, ["report"])
    assert result.exit_code == 0
    assert "ok" in result.output
    assert "unresolved" in result.output


def test_report_shows_latency(runner: CliRunner, project_dir: Path) -> None:
    _write_events(project_dir / ".canon", [_event(latency_ms=50), _event(latency_ms=150)])
    result = runner.invoke(app, ["report"])
    assert result.exit_code == 0
    assert "p50" in result.output
    assert "p95" in result.output


def test_report_json_shape(runner: CliRunner, project_dir: Path) -> None:
    _write_events(
        project_dir / ".canon",
        [_event(latency_ms=42, bytes_scanned=1024, error=None)],
    )
    result = runner.invoke(app, ["--json", "report"])
    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["count"] == 1
    assert payload["error_distribution"] == {"ok": 1}
    assert payload["latency"]["p50_ms"] == 42
    assert payload["bytes_scanned"]["total"] == 1024
    assert payload["telemetry_enabled"] is False


# ---------------------------------------------------------------------------
# --last window
# ---------------------------------------------------------------------------


def test_report_last_window(runner: CliRunner, project_dir: Path) -> None:
    events = [_event(latency_ms=i * 10) for i in range(1, 11)]
    _write_events(project_dir / ".canon", events)

    result = runner.invoke(app, ["--json", "report", "--last", "3"])
    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["count"] == 3


# ---------------------------------------------------------------------------
# telemetry_enabled reflects config (off by default)
# ---------------------------------------------------------------------------


def test_report_telemetry_off_by_default(runner: CliRunner, project_dir: Path) -> None:
    result = runner.invoke(app, ["--json", "report"])
    payload = json.loads(result.output)
    assert payload["telemetry_enabled"] is False
