"""Tests for the event-log read path: read_events and build_report (SPEC-E16 §4)."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from canon.instrumentation.models import AnswerEvent
from canon.instrumentation.report import build_report, read_events

if TYPE_CHECKING:
    from pathlib import Path


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_BASE_EVENT: dict[str, Any] = {
    "ts": "2026-01-01T00:00:00+00:00",
    "kind": "served_answer",
    "contract_schema": "1.4",
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


def _write_events(path: Path, events: list[dict[str, Any]]) -> None:
    path.mkdir(parents=True, exist_ok=True)
    log = path / "events.jsonl"
    log.write_text("\n".join(json.dumps(e, sort_keys=True) for e in events) + "\n")


def _event(**overrides: Any) -> dict[str, Any]:
    return {**_BASE_EVENT, **overrides}


# ---------------------------------------------------------------------------
# read_events
# ---------------------------------------------------------------------------


def test_read_events_missing_file(tmp_path: Path) -> None:
    assert read_events(tmp_path) == []


def test_read_events_empty_log(tmp_path: Path) -> None:
    (tmp_path / ".canon").mkdir()
    (tmp_path / ".canon" / "events.jsonl").write_text("")
    assert read_events(tmp_path) == []


def test_read_events_returns_events(tmp_path: Path) -> None:
    _write_events(tmp_path / ".canon", [_event(), _event(latency_ms=200)])
    events = read_events(tmp_path)
    assert len(events) == 2
    assert all(isinstance(e, AnswerEvent) for e in events)


def test_read_events_skips_malformed_line(tmp_path: Path) -> None:
    log = tmp_path / ".canon" / "events.jsonl"
    log.parent.mkdir(parents=True)
    log.write_text(
        json.dumps(_event(), sort_keys=True)
        + "\n"
        + "not-valid-json\n"
        + json.dumps(_event(latency_ms=300), sort_keys=True)
        + "\n"
    )
    events = read_events(tmp_path)
    assert len(events) == 2


def test_read_events_skips_invalid_schema(tmp_path: Path) -> None:
    log = tmp_path / ".canon" / "events.jsonl"
    log.parent.mkdir(parents=True)
    log.write_text(json.dumps(_event(), sort_keys=True) + "\n" + json.dumps({"ts": "x"}) + "\n")
    events = read_events(tmp_path)
    assert len(events) == 1


def test_read_events_last_window(tmp_path: Path) -> None:
    events_data = [_event(latency_ms=i * 10) for i in range(1, 6)]
    _write_events(tmp_path / ".canon", events_data)
    events = read_events(tmp_path, last=3)
    assert len(events) == 3
    assert events[0].latency_ms == 30
    assert events[-1].latency_ms == 50


# ---------------------------------------------------------------------------
# build_report — empty input
# ---------------------------------------------------------------------------


def test_build_report_empty() -> None:
    rep = build_report([])
    assert rep.count == 0
    assert rep.first_ts is None
    assert rep.last_ts is None
    assert rep.error_distribution == {}
    assert rep.latency is None
    assert rep.bytes_scanned is None
    assert rep.stale_answers == 0
    assert rep.guardrail_coverage == 0
    assert rep.recent == []


# ---------------------------------------------------------------------------
# build_report — error distribution
# ---------------------------------------------------------------------------


def test_build_report_error_distribution() -> None:
    events = [
        AnswerEvent.model_validate(_event(error=None)),
        AnswerEvent.model_validate(_event(error=None)),
        AnswerEvent.model_validate(_event(error="unresolved")),
        AnswerEvent.model_validate(_event(error="connection_failed")),
    ]
    rep = build_report(events)
    assert rep.error_distribution["ok"] == 2
    assert rep.error_distribution["unresolved"] == 1
    assert rep.error_distribution["connection_failed"] == 1


# ---------------------------------------------------------------------------
# build_report — latency percentiles
# ---------------------------------------------------------------------------


def test_build_report_latency_single_event() -> None:
    events = [AnswerEvent.model_validate(_event(latency_ms=42))]
    rep = build_report(events)
    assert rep.latency is not None
    assert rep.latency.min_ms == 42
    assert rep.latency.max_ms == 42
    assert rep.latency.p50_ms == 42
    assert rep.latency.p95_ms == 42
    assert rep.latency.avg_ms == 42.0


def test_build_report_latency_known_set() -> None:
    # [10, 20, 30, 40, 50, 60, 70, 80, 90, 100] → p50=50, p95=100
    events = [
        AnswerEvent.model_validate(_event(latency_ms=v))
        for v in [10, 20, 30, 40, 50, 60, 70, 80, 90, 100]
    ]
    rep = build_report(events)
    assert rep.latency is not None
    assert rep.latency.min_ms == 10
    assert rep.latency.max_ms == 100
    assert rep.latency.p50_ms == 50
    assert rep.latency.p95_ms == 100
    assert rep.latency.avg_ms == 55.0


# ---------------------------------------------------------------------------
# build_report — bytes scanned
# ---------------------------------------------------------------------------


def test_build_report_bytes_all_null() -> None:
    events = [AnswerEvent.model_validate(_event(bytes_scanned=None))]
    rep = build_report(events)
    assert rep.bytes_scanned is None


def test_build_report_bytes_summary() -> None:
    events = [
        AnswerEvent.model_validate(_event(bytes_scanned=100)),
        AnswerEvent.model_validate(_event(bytes_scanned=None)),
        AnswerEvent.model_validate(_event(bytes_scanned=300)),
    ]
    rep = build_report(events)
    assert rep.bytes_scanned is not None
    assert rep.bytes_scanned.total == 400
    assert rep.bytes_scanned.min == 100
    assert rep.bytes_scanned.max == 300
    assert rep.bytes_scanned.avg == 200.0


# ---------------------------------------------------------------------------
# build_report — freshness and guardrails
# ---------------------------------------------------------------------------


def test_build_report_stale_answers() -> None:
    events = [
        AnswerEvent.model_validate(
            _event(freshness=[{"source": "a", "stale": False, "age_days": 0}])
        ),
        AnswerEvent.model_validate(
            _event(freshness=[{"source": "b", "stale": True, "age_days": 5}])
        ),
        AnswerEvent.model_validate(_event(freshness=[])),
    ]
    rep = build_report(events)
    assert rep.stale_answers == 1


def test_build_report_guardrail_coverage() -> None:
    events = [
        AnswerEvent.model_validate(_event(guardrails_fired=[])),
        AnswerEvent.model_validate(_event(guardrails_fired=["g1"])),
        AnswerEvent.model_validate(_event(guardrails_fired=["g1", "g2"])),
    ]
    rep = build_report(events)
    assert rep.guardrail_coverage == 2


# ---------------------------------------------------------------------------
# build_report — recent window
# ---------------------------------------------------------------------------


def test_build_report_recent_default() -> None:
    events = [AnswerEvent.model_validate(_event(latency_ms=i)) for i in range(15)]
    rep = build_report(events, recent=10)
    assert len(rep.recent) == 10
    assert rep.recent[0].latency_ms == 5


def test_build_report_recent_less_than_total() -> None:
    events = [AnswerEvent.model_validate(_event(latency_ms=i)) for i in range(3)]
    rep = build_report(events, recent=10)
    assert len(rep.recent) == 3


# ---------------------------------------------------------------------------
# build_report — timestamps
# ---------------------------------------------------------------------------


def test_build_report_timestamps() -> None:
    events = [
        AnswerEvent.model_validate(_event(ts="2026-01-01T00:00:00+00:00")),
        AnswerEvent.model_validate(_event(ts="2026-01-02T00:00:00+00:00")),
    ]
    rep = build_report(events)
    assert rep.first_ts == "2026-01-01T00:00:00+00:00"
    assert rep.last_ts == "2026-01-02T00:00:00+00:00"
