"""Tests for events.jsonl size rotation, retention and multi-segment reads (SPEC-E16 §12)."""

from __future__ import annotations

import gzip
import json
import multiprocessing
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from canonic.config import EventLogConfig
from canonic.instrumentation.events import (
    DiskAnswerEventLog,
    emit_milestone_once,
    event_log_paths,
)
from canonic.instrumentation.models import (
    AnswerEvent,
    FunnelEvent,
    FunnelMilestone,
)
from canonic.instrumentation.report import read_events

_ANSWER: dict[str, Any] = {
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


def _answer(latency_ms: int = 1) -> AnswerEvent:
    return AnswerEvent.model_validate({**_ANSWER, "latency_ms": latency_ms})


def _funnel(milestone: FunnelMilestone) -> FunnelEvent:
    return FunnelEvent(ts=datetime.now(UTC).isoformat(), milestone=milestone)


def _segments(root: Path) -> list[Path]:
    return sorted((root / ".canonic").glob("events-*.jsonl*"))


def test_no_rotation_below_limit(tmp_path: Path) -> None:
    log = DiskAnswerEventLog(tmp_path, EventLogConfig(max_bytes=10_000_000))
    for i in range(5):
        log.append(_answer(i))
    assert _segments(tmp_path) == []
    assert len(read_events(tmp_path, kind="served_answer")) == 5


def test_max_bytes_zero_disables_rotation(tmp_path: Path) -> None:
    log = DiskAnswerEventLog(tmp_path, EventLogConfig(max_bytes=0))
    for i in range(20):
        log.append(_answer(i))
    assert _segments(tmp_path) == []


def test_rotation_moves_served_answers_to_gzip_segment(tmp_path: Path) -> None:
    log = DiskAnswerEventLog(tmp_path, EventLogConfig(max_bytes=500))
    for i in range(10):
        log.append(_answer(i))

    segments = _segments(tmp_path)
    assert segments
    assert all(p.suffix == ".gz" for p in segments)
    with gzip.open(segments[0], "rt", encoding="utf-8") as f:
        first = json.loads(f.readline())
    assert first["kind"] == "served_answer"
    assert len(read_events(tmp_path, kind="served_answer")) == 10


def test_uncompressed_segments(tmp_path: Path) -> None:
    log = DiskAnswerEventLog(tmp_path, EventLogConfig(max_bytes=500, compress=False))
    for i in range(10):
        log.append(_answer(i))
    segments = _segments(tmp_path)
    assert segments
    assert all(p.suffix == ".jsonl" for p in segments)
    assert len(read_events(tmp_path)) == 10


def test_durable_events_stay_in_active_file(tmp_path: Path) -> None:
    log = DiskAnswerEventLog(tmp_path, EventLogConfig(max_bytes=500))
    log.append(_funnel(FunnelMilestone.SETUP_STARTED))
    for i in range(10):
        log.append(_answer(i))

    active = (tmp_path / ".canonic" / "events.jsonl").read_text()
    assert FunnelMilestone.SETUP_STARTED in {
        e.milestone for e in read_events(tmp_path, kind="funnel_milestone")
    }
    assert "funnel_milestone" in active
    for segment in _segments(tmp_path):
        with gzip.open(segment, "rt", encoding="utf-8") as f:
            assert all(json.loads(line)["kind"] == "served_answer" for line in f)


def test_emit_milestone_once_stays_idempotent_after_rotation(tmp_path: Path) -> None:
    log = DiskAnswerEventLog(tmp_path, EventLogConfig(max_bytes=500))
    emit_milestone_once(tmp_path, FunnelMilestone.SETUP_STARTED)
    for i in range(20):
        log.append(_answer(i))
    assert _segments(tmp_path)

    emit_milestone_once(tmp_path, FunnelMilestone.SETUP_STARTED)
    assert len(read_events(tmp_path, kind="funnel_milestone")) == 1


def test_last_spans_segments_in_order(tmp_path: Path) -> None:
    log = DiskAnswerEventLog(tmp_path, EventLogConfig(max_bytes=500))
    for i in range(30):
        log.append(_answer(i))
    assert len(_segments(tmp_path)) >= 2

    events = read_events(tmp_path, last=7, kind="served_answer")
    assert [e.latency_ms for e in events] == list(range(23, 30))


def test_max_segments_deletes_oldest(tmp_path: Path) -> None:
    log = DiskAnswerEventLog(tmp_path, EventLogConfig(max_bytes=500, max_segments=1))
    for i in range(40):
        log.append(_answer(i))

    assert len(_segments(tmp_path)) == 1
    events = read_events(tmp_path, kind="served_answer")
    assert len(events) < 40
    assert events[-1].latency_ms == 39


def test_retention_days_deletes_expired_segments_only(tmp_path: Path) -> None:
    state = tmp_path / ".canonic"
    state.mkdir()
    old = datetime.now(UTC) - timedelta(days=30)
    old_segment = state / f"events-{old.strftime('%Y%m%dT%H%M%S%fZ')}.jsonl"
    old_segment.write_text(json.dumps(_ANSWER) + "\n")
    unknown = state / "events-legacy.jsonl"
    unknown.write_text(json.dumps(_ANSWER) + "\n")

    log = DiskAnswerEventLog(tmp_path, EventLogConfig(max_bytes=500, retention_days=7))
    for i in range(10):
        log.append(_answer(i))

    assert not old_segment.exists()
    assert unknown.exists()
    assert len(_segments(tmp_path)) >= 2


def test_non_served_kind_skips_segments(tmp_path: Path) -> None:
    log = DiskAnswerEventLog(tmp_path, EventLogConfig(max_bytes=500))
    for i in range(10):
        log.append(_answer(i))
    assert _segments(tmp_path)
    assert event_log_paths(tmp_path, include_segments=False) == [
        tmp_path / ".canonic" / "events.jsonl"
    ]


def _append_many(root: str, worker: int) -> None:
    log = DiskAnswerEventLog(Path(root), EventLogConfig(max_bytes=2_000))
    for i in range(50):
        log.append(_answer(worker * 1000 + i))


def test_concurrent_writers_lose_no_events(tmp_path: Path) -> None:
    ctx = multiprocessing.get_context("spawn")
    procs = [ctx.Process(target=_append_many, args=(str(tmp_path), w)) for w in range(4)]
    for p in procs:
        p.start()
    for p in procs:
        p.join(timeout=60)
        assert p.exitcode == 0

    latencies = sorted(e.latency_ms for e in read_events(tmp_path, kind="served_answer"))
    expected = sorted(w * 1000 + i for w in range(4) for i in range(50))
    assert latencies == expected
