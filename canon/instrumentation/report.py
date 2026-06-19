"""Read path for the local event log — aggregates served-answer records (SPEC-E16 §4, §11 S4)."""

from __future__ import annotations

import json
import math
from json import JSONDecodeError
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict, ValidationError

from canon.config import LOCAL_STATE_DIR
from canon.instrumentation.events import _EVENTS_FILE
from canon.instrumentation.models import AnswerEvent, ReconcileDecisionEvent

if TYPE_CHECKING:
    from pathlib import Path

    from canon.instrumentation.events import CanonEvent

__all__ = ["EventReport", "LatencySummary", "BytesSummary", "read_events", "build_report"]


class LatencySummary(BaseModel):
    model_config = ConfigDict(frozen=True)

    min_ms: int
    max_ms: int
    avg_ms: float
    p50_ms: int
    p95_ms: int


class BytesSummary(BaseModel):
    model_config = ConfigDict(frozen=True)

    total: int
    min: int
    max: int
    avg: float


class EventReport(BaseModel):
    model_config = ConfigDict(frozen=True)

    count: int
    first_ts: str | None
    last_ts: str | None
    error_distribution: dict[str, int]
    latency: LatencySummary | None
    bytes_scanned: BytesSummary | None
    stale_answers: int
    guardrail_coverage: int
    recent: list[AnswerEvent]


def _percentile(sorted_values: list[int], p: float) -> int:
    """Nearest-rank percentile on a pre-sorted list (must be non-empty)."""
    n = len(sorted_values)
    rank = math.ceil(p / 100.0 * n)
    return sorted_values[rank - 1]


def _parse_line(line: str) -> CanonEvent | None:
    """Parse one NDJSON line into the appropriate event type; return None on failure."""
    try:
        data = json.loads(line)
    except JSONDecodeError:
        return None
    kind = data.get("kind")
    try:
        if kind == "served_answer":
            return AnswerEvent.model_validate(data)
        if kind == "reconcile_decision":
            return ReconcileDecisionEvent.model_validate(data)
    except ValidationError:
        pass
    return None


def read_events(
    project_root: Path,
    last: int | None = None,
    kind: Literal["served_answer", "reconcile_decision"] | None = None,
) -> list[CanonEvent]:
    """Read and parse events from the local event log.

    Returns an empty list if the log file is missing. Malformed or unknown lines
    are skipped. Pass ``kind`` to filter to one event type.
    """
    log_path = project_root / LOCAL_STATE_DIR / _EVENTS_FILE
    if not log_path.exists():
        return []

    lines = log_path.read_text().splitlines()
    if last is not None:
        lines = lines[-last:]

    events: list[CanonEvent] = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        event = _parse_line(line)
        if event is None:
            continue
        if kind is not None and event.kind != kind:
            continue
        events.append(event)
    return events


def build_report(events: list[AnswerEvent], recent: int = 10) -> EventReport:
    """Aggregate a list of AnswerEvents into an EventReport."""
    if not events:
        return EventReport(
            count=0,
            first_ts=None,
            last_ts=None,
            error_distribution={},
            latency=None,
            bytes_scanned=None,
            stale_answers=0,
            guardrail_coverage=0,
            recent=[],
        )

    error_dist: dict[str, int] = {}
    for event in events:
        key = event.error if event.error is not None else "ok"
        error_dist[key] = error_dist.get(key, 0) + 1

    latencies = sorted(e.latency_ms for e in events)
    latency = LatencySummary(
        min_ms=latencies[0],
        max_ms=latencies[-1],
        avg_ms=sum(latencies) / len(latencies),
        p50_ms=_percentile(latencies, 50),
        p95_ms=_percentile(latencies, 95),
    )

    bytes_values = [e.bytes_scanned for e in events if e.bytes_scanned is not None]
    bytes_summary: BytesSummary | None = None
    if bytes_values:
        bytes_summary = BytesSummary(
            total=sum(bytes_values),
            min=min(bytes_values),
            max=max(bytes_values),
            avg=sum(bytes_values) / len(bytes_values),
        )

    stale_answers = sum(1 for e in events if any(f.get("stale") for f in e.freshness))
    guardrail_coverage = sum(1 for e in events if e.guardrails_fired)

    return EventReport(
        count=len(events),
        first_ts=events[0].ts,
        last_ts=events[-1].ts,
        error_distribution=error_dist,
        latency=latency,
        bytes_scanned=bytes_summary,
        stale_answers=stale_answers,
        guardrail_coverage=guardrail_coverage,
        recent=events[-recent:],
    )
