"""Tests for OB-S6 funnel events: FunnelEvent round-trip, emit helpers, and build_funnel."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from canon.instrumentation.events import DiskAnswerEventLog, emit_milestone, emit_milestone_once
from canon.instrumentation.models import FunnelEvent, FunnelMilestone
from canon.instrumentation.report import build_funnel, read_events

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_EVENTS_FILE = Path(".canon") / "events.jsonl"


def _read_raw_lines(root: Path) -> list[dict[str, Any]]:
    path = root / _EVENTS_FILE
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def _funnel_events(root: Path) -> list[FunnelEvent]:
    return read_events(root, kind="funnel_milestone")


def _ts(offset_s: float = 0.0) -> str:
    return (datetime.now(UTC) + timedelta(seconds=offset_s)).isoformat()


# ---------------------------------------------------------------------------
# FunnelEvent model
# ---------------------------------------------------------------------------


def test_funnel_event_round_trips_through_json() -> None:
    ts = _ts()
    event = FunnelEvent(ts=ts, milestone=FunnelMilestone.SETUP_STARTED)
    dumped = event.model_dump(mode="json")
    assert dumped["kind"] == "funnel_milestone"
    assert dumped["milestone"] == "setup_started"
    assert dumped["ts"] == ts
    restored = FunnelEvent.model_validate(dumped)
    assert restored == event


def test_funnel_event_is_content_free() -> None:
    event = FunnelEvent(ts=_ts(), milestone=FunnelMilestone.FIRST_ANSWER_SERVED)
    dumped = event.model_dump(mode="json")
    # Only ts, kind, milestone — no warehouse content, hashes, or user data
    assert set(dumped.keys()) == {"ts", "kind", "milestone"}


def test_funnel_milestone_values_are_lowercase() -> None:
    for milestone in FunnelMilestone:
        assert milestone.value == milestone.value.lower()


# ---------------------------------------------------------------------------
# emit_milestone / DiskAnswerEventLog round-trip
# ---------------------------------------------------------------------------


def test_emit_milestone_writes_to_disk(tmp_path: Path) -> None:
    log = DiskAnswerEventLog(tmp_path)
    emit_milestone(log, FunnelMilestone.SETUP_STARTED)

    lines = _read_raw_lines(tmp_path)
    assert len(lines) == 1
    assert lines[0]["kind"] == "funnel_milestone"
    assert lines[0]["milestone"] == "setup_started"


def test_emit_milestone_swallows_errors(tmp_path: Path) -> None:
    class _BrokenLog:
        def append(self, event: Any) -> None:
            raise RuntimeError("disk full")

    # must not raise
    emit_milestone(_BrokenLog(), FunnelMilestone.CONNECTION_ADDED)  # type: ignore[arg-type]


def test_emit_milestone_once_fires_first_time(tmp_path: Path) -> None:
    emit_milestone_once(tmp_path, FunnelMilestone.BOOTSTRAP_COMPLETED)
    events = _funnel_events(tmp_path)
    assert len(events) == 1
    assert events[0].milestone == FunnelMilestone.BOOTSTRAP_COMPLETED


def test_emit_milestone_once_is_idempotent(tmp_path: Path) -> None:
    emit_milestone_once(tmp_path, FunnelMilestone.BOOTSTRAP_COMPLETED)
    emit_milestone_once(tmp_path, FunnelMilestone.BOOTSTRAP_COMPLETED)
    emit_milestone_once(tmp_path, FunnelMilestone.BOOTSTRAP_COMPLETED)
    events = _funnel_events(tmp_path)
    assert len(events) == 1


def test_multiple_different_milestones_all_recorded(tmp_path: Path) -> None:
    log = DiskAnswerEventLog(tmp_path)
    for milestone in FunnelMilestone:
        emit_milestone(log, milestone)
    events = _funnel_events(tmp_path)
    assert [e.milestone for e in events] == list(FunnelMilestone)


# ---------------------------------------------------------------------------
# read_events with kind="funnel_milestone"
# ---------------------------------------------------------------------------


def test_read_events_funnel_milestone_filters_other_kinds(tmp_path: Path) -> None:
    canon_dir = tmp_path / ".canon"
    canon_dir.mkdir()
    log_path = canon_dir / "events.jsonl"
    other_event = {
        "ts": _ts(),
        "kind": "served_answer",
        "contract_schema": "v1",
        "query_hash": "sha256:abc",
        "compiled_sql_hash": None,
        "connection": None,
        "resolved": {},
        "guardrails_fired": [],
        "finality": None,
        "freshness": [],
        "latency_ms": 50,
        "bytes_scanned": None,
        "error": None,
        "trust_score": None,
        "cache_hit": None,
        "over_limit_blocked": None,
    }
    funnel_event = {"ts": _ts(), "kind": "funnel_milestone", "milestone": "setup_started"}
    log_path.write_text(
        json.dumps(other_event, sort_keys=True)
        + "\n"
        + json.dumps(funnel_event, sort_keys=True)
        + "\n"
    )
    events = read_events(tmp_path, kind="funnel_milestone")
    assert len(events) == 1
    assert events[0].milestone == FunnelMilestone.SETUP_STARTED


def test_read_events_skips_malformed_funnel_line(tmp_path: Path) -> None:
    canon_dir = tmp_path / ".canon"
    canon_dir.mkdir()
    log_path = canon_dir / "events.jsonl"
    log_path.write_text(
        '{"kind": "funnel_milestone", "ts": "2026-01-01T00:00:00Z", "milestone": "unknown_milestone"}\n'
        '{"kind": "funnel_milestone", "ts": "2026-01-01T00:00:00Z", "milestone": "setup_started"}\n'
    )
    events = read_events(tmp_path, kind="funnel_milestone")
    assert len(events) == 1
    assert events[0].milestone == FunnelMilestone.SETUP_STARTED


# ---------------------------------------------------------------------------
# build_funnel
# ---------------------------------------------------------------------------


def _make_events(*milestones: FunnelMilestone, base_offset: float = 0.0) -> list[FunnelEvent]:
    return [
        FunnelEvent(ts=_ts(base_offset + i * 10), milestone=m) for i, m in enumerate(milestones)
    ]


def test_build_funnel_empty_returns_empty_report() -> None:
    report = build_funnel([])
    assert report.reached == []
    assert report.milestones == {}
    assert report.time_to_first_answer_seconds is None
    assert report.dropped_after is None


def test_build_funnel_partial_funnel() -> None:
    events = _make_events(
        FunnelMilestone.SETUP_STARTED,
        FunnelMilestone.CONNECTION_ADDED,
        FunnelMilestone.BOOTSTRAP_COMPLETED,
    )
    report = build_funnel(events)
    assert report.reached == [
        "setup_started",
        "connection_added",
        "bootstrap_completed",
    ]
    assert report.dropped_after == "bootstrap_completed"
    assert report.time_to_first_answer_seconds is None


def test_build_funnel_time_to_first_answer_computable(tmp_path: Path) -> None:
    t0 = datetime.now(UTC)
    t1 = t0 + timedelta(seconds=42.5)
    events = [
        FunnelEvent(ts=t0.isoformat(), milestone=FunnelMilestone.SETUP_STARTED),
        FunnelEvent(ts=t1.isoformat(), milestone=FunnelMilestone.FIRST_ANSWER_SERVED),
    ]
    report = build_funnel(events)
    assert report.time_to_first_answer_seconds is not None
    assert abs(report.time_to_first_answer_seconds - 42.5) < 0.1


def test_build_funnel_all_milestones_no_dropped_after() -> None:
    events = _make_events(*list(FunnelMilestone))
    report = build_funnel(events)
    assert len(report.reached) == len(list(FunnelMilestone))
    assert report.dropped_after is None


def test_build_funnel_uses_first_ts_per_milestone() -> None:
    t0 = datetime.now(UTC)
    t1 = t0 + timedelta(seconds=5)
    events = [
        FunnelEvent(ts=t0.isoformat(), milestone=FunnelMilestone.SETUP_STARTED),
        FunnelEvent(ts=t1.isoformat(), milestone=FunnelMilestone.SETUP_STARTED),
    ]
    report = build_funnel(events)
    assert report.milestones["setup_started"] == t0.isoformat()


def test_build_funnel_ordered_regardless_of_emission_order() -> None:
    events = _make_events(
        FunnelMilestone.BOOTSTRAP_COMPLETED,
        FunnelMilestone.CONNECTION_ADDED,
        FunnelMilestone.SETUP_STARTED,
    )
    report = build_funnel(events)
    assert report.reached == ["setup_started", "connection_added", "bootstrap_completed"]
