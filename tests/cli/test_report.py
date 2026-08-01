"""Tests for ``canonic report``."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import pytest

from canonic.cli.app import app

if TYPE_CHECKING:
    from pathlib import Path

    from typer.testing import CliRunner

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


def _event(**overrides: Any) -> dict[str, Any]:
    return {**_BASE_EVENT, **overrides}


def _write_events(dotcanonic: Path, events: list[dict[str, Any]]) -> None:
    dotcanonic.mkdir(parents=True, exist_ok=True)
    (dotcanonic / "events.jsonl").write_text(
        "\n".join(json.dumps(e, sort_keys=True) for e in events) + "\n"
    )


# ---------------------------------------------------------------------------
# Outside project
# ---------------------------------------------------------------------------


def test_report_outside_project(runner: CliRunner, tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["report"])
    assert result.exit_code == 0
    assert "no canonic project found" in result.output


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
        project_dir / ".canonic",
        [_event(latency_ms=100), _event(latency_ms=200, error="unresolved")],
    )
    result = runner.invoke(app, ["report"])
    assert result.exit_code == 0
    assert "2" in result.output


def test_report_shows_error_distribution(runner: CliRunner, project_dir: Path) -> None:
    _write_events(
        project_dir / ".canonic",
        [_event(), _event(error="unresolved"), _event(error="unresolved")],
    )
    result = runner.invoke(app, ["report"])
    assert result.exit_code == 0
    assert "ok" in result.output
    assert "unresolved" in result.output


def test_report_shows_latency(runner: CliRunner, project_dir: Path) -> None:
    _write_events(project_dir / ".canonic", [_event(latency_ms=50), _event(latency_ms=150)])
    result = runner.invoke(app, ["report"])
    assert result.exit_code == 0
    assert "p50" in result.output
    assert "p95" in result.output


def test_report_json_shape(runner: CliRunner, project_dir: Path) -> None:
    _write_events(
        project_dir / ".canonic",
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
    _write_events(project_dir / ".canonic", events)

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


# ---------------------------------------------------------------------------
# OB-S6: funnel section in canonic report
# ---------------------------------------------------------------------------


def _funnel_event(milestone: str, ts: str = "2026-01-01T00:00:00+00:00") -> dict[str, Any]:
    return {"kind": "funnel_milestone", "milestone": milestone, "ts": ts}


def test_report_funnel_section_shown_when_milestones_present(
    runner: CliRunner, project_dir: Path
) -> None:
    _write_events(
        project_dir / ".canonic",
        [
            _funnel_event("setup_started", "2026-01-01T00:00:00+00:00"),
            _funnel_event("connection_added", "2026-01-01T00:00:10+00:00"),
            _funnel_event("first_answer_served", "2026-01-01T00:00:42+00:00"),
        ],
    )
    result = runner.invoke(app, ["report"])
    assert result.exit_code == 0
    assert "onboarding funnel" in result.output
    assert "setup_started" in result.output
    assert "connection_added" in result.output
    assert "time-to-first-answer" in result.output


def test_report_funnel_section_hidden_when_no_milestones(
    runner: CliRunner, project_dir: Path
) -> None:
    _write_events(project_dir / ".canonic", [_event(latency_ms=10)])
    result = runner.invoke(app, ["report"])
    assert result.exit_code == 0
    assert "onboarding funnel" not in result.output


def test_report_json_includes_funnel(runner: CliRunner, project_dir: Path) -> None:
    _write_events(
        project_dir / ".canonic",
        [
            _funnel_event("setup_started", "2026-01-01T00:00:00+00:00"),
            _funnel_event("first_answer_served", "2026-01-01T00:01:30+00:00"),
        ],
    )
    result = runner.invoke(app, ["--json", "report"])
    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert "funnel" in payload
    assert "setup_started" in payload["funnel"]["milestones"]
    assert payload["funnel"]["time_to_first_answer_seconds"] == pytest.approx(90.0, abs=1.0)


def test_report_funnel_time_to_first_answer_none_when_missing_milestone(
    runner: CliRunner, project_dir: Path
) -> None:
    _write_events(project_dir / ".canonic", [_funnel_event("setup_started")])
    result = runner.invoke(app, ["--json", "report"])
    payload = json.loads(result.output)
    assert payload["funnel"]["time_to_first_answer_seconds"] is None


# ---------------------------------------------------------------------------
# SPEC-E16 Part 2 §4 — trust calibration + correction recurrence
# ---------------------------------------------------------------------------


def _outcome_event(
    ref: str, verdict: str, ts: str = "2026-01-01T00:01:00+00:00", **overrides: Any
) -> dict[str, Any]:
    return {
        "kind": "answer_outcome",
        "ts": ts,
        "ref": ref,
        "verdict": verdict,
        "marked_by": "analyst",
        **overrides,
    }


def test_report_shows_calibration_when_outcomes_present(
    runner: CliRunner, project_dir: Path
) -> None:
    _write_events(
        project_dir / ".canonic",
        [
            _event(query_hash="sha256:1", trust_score="caution"),
            _outcome_event("sha256:1", "incorrect", reason_code="wrong_definition"),
        ],
    )
    result = runner.invoke(app, ["report"])
    assert result.exit_code == 0
    assert "Trust calibration" in result.output
    assert "caution" in result.output


def test_report_json_includes_calibration_and_recurrence(
    runner: CliRunner, project_dir: Path
) -> None:
    _write_events(
        project_dir / ".canonic",
        [
            _event(query_hash="sha256:1", trust_score="caution"),
            _outcome_event("sha256:1", "incorrect", reason_code="wrong_definition"),
        ],
    )
    result = runner.invoke(app, ["--json", "report"])
    payload = json.loads(result.output)
    assert payload["calibration"]["buckets"][0]["tier"] == "caution"
    assert payload["calibration"]["buckets"][0]["incorrect"] == 1
    assert "correction_recurrence" in payload


def test_report_shows_recurrence_for_repeated_binding(runner: CliRunner, project_dir: Path) -> None:
    _write_events(
        project_dir / ".canonic",
        [
            _event(
                query_hash="sha256:1",
                resolved={"metrics": {"revenue": "orders.total_revenue"}},
            ),
            _event(
                query_hash="sha256:2",
                resolved={"metrics": {"revenue": "orders.total_revenue"}},
            ),
            _outcome_event("sha256:1", "incorrect"),
            _outcome_event("sha256:2", "incorrect"),
        ],
    )
    result = runner.invoke(app, ["report"])
    assert result.exit_code == 0
    assert "Correction recurrence" in result.output
    assert "orders.total_revenue" in result.output


# ---------------------------------------------------------------------------
# SPEC-E11 §6 — Feedback loop section (S5-AC1)
# ---------------------------------------------------------------------------


def _recent_ts(days_ago: float = 1) -> str:
    from datetime import UTC, datetime, timedelta

    return (datetime.now(UTC) - timedelta(days=days_ago)).isoformat()


def test_report_shows_feedback_section_for_recurring_pattern(
    runner: CliRunner, project_dir: Path
) -> None:
    ts = _recent_ts()
    _write_events(
        project_dir / ".canonic",
        [
            _event(
                query_hash="sha256:1",
                resolved={"metrics": {"revenue": "orders.total_revenue"}},
            ),
            _event(
                query_hash="sha256:2",
                resolved={"metrics": {"revenue": "orders.total_revenue"}},
            ),
            _outcome_event("sha256:1", "incorrect", ts=ts, reason_code="wrong_definition"),
            _outcome_event("sha256:2", "incorrect", ts=ts, reason_code="wrong_definition"),
        ],
    )
    result = runner.invoke(app, ["report"])
    assert result.exit_code == 0
    assert "Feedback loop" in result.output
    assert "orders.total_revenue" in result.output


def test_report_feedback_section_hidden_without_wrong_definition_history(
    runner: CliRunner, project_dir: Path
) -> None:
    _write_events(project_dir / ".canonic", [_event(latency_ms=10)])
    result = runner.invoke(app, ["report"])
    assert result.exit_code == 0
    assert "Feedback loop" not in result.output


def test_report_json_feedback_reflects_gate_and_cap(runner: CliRunner, project_dir: Path) -> None:
    ts = _recent_ts()
    _write_events(
        project_dir / ".canonic",
        [
            _event(
                query_hash="sha256:1",
                resolved={"metrics": {"revenue": "orders.total_revenue"}},
            ),
            _event(
                query_hash="sha256:2",
                resolved={"metrics": {"revenue": "orders.total_revenue"}},
            ),
            _outcome_event("sha256:1", "incorrect", ts=ts, reason_code="wrong_definition"),
            _outcome_event("sha256:2", "incorrect", ts=ts, reason_code="wrong_definition"),
        ],
    )
    result = runner.invoke(app, ["--json", "report"])
    payload = json.loads(result.output)
    entries = payload["feedback"]["entries"]
    assert len(entries) == 1
    entry = entries[0]
    assert entry["binding"] == "orders.total_revenue"
    assert entry["wrong_definition_count"] == 2
    assert entry["gated"] is True
    assert entry["capped"] is True
    assert entry["refs"] == ["sha256:1", "sha256:2"]


def test_report_json_feedback_single_incident_not_gated(
    runner: CliRunner, project_dir: Path
) -> None:
    """S2-AC1: a single incident is visible in the audit but never gated."""
    ts = _recent_ts()
    _write_events(
        project_dir / ".canonic",
        [
            _event(
                query_hash="sha256:1",
                resolved={"metrics": {"revenue": "orders.total_revenue"}},
            ),
            _outcome_event("sha256:1", "incorrect", ts=ts, reason_code="wrong_definition"),
        ],
    )
    result = runner.invoke(app, ["--json", "report"])
    payload = json.loads(result.output)
    entries = payload["feedback"]["entries"]
    assert len(entries) == 1
    assert entries[0]["gated"] is False


def test_report_json_feedback_ignores_wrong_data(runner: CliRunner, project_dir: Path) -> None:
    """S1: wrong_data outcomes never surface in the feedback audit."""
    ts = _recent_ts()
    _write_events(
        project_dir / ".canonic",
        [
            _event(
                query_hash="sha256:1",
                resolved={"metrics": {"revenue": "orders.total_revenue"}},
            ),
            _outcome_event("sha256:1", "incorrect", ts=ts, reason_code="wrong_data"),
        ],
    )
    result = runner.invoke(app, ["--json", "report"])
    payload = json.loads(result.output)
    assert payload["feedback"]["entries"] == []


# ---------------------------------------------------------------------------
# SPEC-E16 Part 2 §5 — --telemetry-preview
# ---------------------------------------------------------------------------


def test_telemetry_preview_shows_payload(runner: CliRunner, project_dir: Path) -> None:
    _write_events(project_dir / ".canonic", [_event(latency_ms=100)])
    result = runner.invoke(app, ["report", "--telemetry-preview"])
    assert result.exit_code == 0
    assert "telemetry preview" in result.output
    assert "nothing is sent" in result.output


def test_telemetry_preview_json_content_safe(runner: CliRunner, project_dir: Path) -> None:
    _write_events(
        project_dir / ".canonic",
        [_event(query_hash="sha256:super-secret", compiled_sql_hash="sha256:sql-secret")],
    )
    result = runner.invoke(app, ["--json", "report", "--telemetry-preview"])
    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["schema_version"] == "1"
    assert payload["answer_count"] == 1
    dumped = json.dumps(payload)
    assert "sha256:super-secret" not in dumped
    assert "sha256:sql-secret" not in dumped
    assert "query_hash" not in payload
    assert "resolved" not in payload


def test_telemetry_preview_does_not_send_anything(runner: CliRunner, project_dir: Path) -> None:
    """--telemetry-preview must remain purely local and side-effect-free."""
    _write_events(project_dir / ".canonic", [_event()])
    before = (project_dir / ".canonic" / "events.jsonl").read_text()
    runner.invoke(app, ["report", "--telemetry-preview"])
    after = (project_dir / ".canonic" / "events.jsonl").read_text()
    assert before == after


# ---------------------------------------------------------------------------
# --telemetry-send
# ---------------------------------------------------------------------------

_CONFIG_WITH_TELEMETRY_AUTHORIZED = """\
version: 1
project:
  name: test-project
llm:
  provider: openai_compatible
  base_url: http://localhost:11434/v1
  model: llama3
telemetry:
  enabled: true
  endpoint: https://collector.example.com/ingest
  transport_acknowledged: true
"""


def test_telemetry_preview_and_send_are_mutually_exclusive(
    runner: CliRunner, project_dir: Path
) -> None:
    result = runner.invoke(app, ["report", "--telemetry-preview", "--telemetry-send"])
    assert result.exit_code == 2
    assert "mutually" in result.output
    assert "exclusive" in result.output


def test_telemetry_send_fails_closed_without_config(runner: CliRunner, project_dir: Path) -> None:
    """Default project_dir config has telemetry disabled — send must refuse, not no-op silently."""
    _write_events(project_dir / ".canonic", [_event()])
    result = runner.invoke(app, ["report", "--telemetry-send"])
    assert result.exit_code == 20


def test_telemetry_send_calls_transport_and_leaves_log_untouched(
    runner: CliRunner, project_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (project_dir / "canonic.yaml").write_text(_CONFIG_WITH_TELEMETRY_AUTHORIZED)
    _write_events(project_dir / ".canonic", [_event()])
    before = (project_dir / ".canonic" / "events.jsonl").read_text()

    calls: list[tuple[dict[str, Any], dict[str, Any]]] = []

    async def fake_send_telemetry(payload: dict[str, Any], **kwargs: Any) -> None:
        calls.append((payload, kwargs))

    monkeypatch.setattr("canonic.cli.commands.report.send_telemetry", fake_send_telemetry)

    result = runner.invoke(app, ["report", "--telemetry-send"])

    assert result.exit_code == 0, result.output
    assert len(calls) == 1
    payload, kwargs = calls[0]
    assert payload["schema_version"] == "1"
    assert kwargs["endpoint"] == "https://collector.example.com/ingest"
    after = (project_dir / ".canonic" / "events.jsonl").read_text()
    assert before == after


# ---------------------------------------------------------------------------
# --bundle diagnostic export
# ---------------------------------------------------------------------------


def test_bundle_writes_json_file(runner: CliRunner, project_dir: Path) -> None:
    _write_events(project_dir / ".canonic", [_event(latency_ms=42)])
    out = project_dir / "diag.json"
    result = runner.invoke(app, ["report", "--bundle", str(out)])
    assert result.exit_code == 0
    assert out.exists()
    payload = json.loads(out.read_text())
    assert payload["report"]["count"] == 1
    assert "canonic_version" in payload
    assert "written to" in result.output


def test_bundle_message_notes_no_credentials(runner: CliRunner, project_dir: Path) -> None:
    out = project_dir / "diag.json"
    result = runner.invoke(app, ["report", "--bundle", str(out)])
    assert result.exit_code == 0
    assert "no query results or credentials" in result.output


def test_bundle_does_not_write_normal_report_output(runner: CliRunner, project_dir: Path) -> None:
    """--bundle short-circuits before the on-screen report tables are rendered."""
    _write_events(
        project_dir / ".canonic",
        [_event(), _event(error="unresolved")],
    )
    out = project_dir / "diag.json"
    result = runner.invoke(app, ["report", "--bundle", str(out)])
    assert result.exit_code == 0
    assert "Error distribution" not in result.output


def test_bundle_outside_project_reports_no_project(
    runner: CliRunner, tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["report", "--bundle", str(tmp_path / "diag.json")])
    assert result.exit_code == 0
    assert "no canonic project found" in result.output
    assert not (tmp_path / "diag.json").exists()
