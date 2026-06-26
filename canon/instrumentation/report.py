"""Read path for the local event log — aggregates served-answer records (SPEC-E16 §4, §11 S4/S6)."""

from __future__ import annotations

import json
import math
from datetime import UTC, datetime
from json import JSONDecodeError
from typing import TYPE_CHECKING, Literal, overload

from pydantic import BaseModel, ConfigDict, ValidationError

from canon.config import LOCAL_STATE_DIR
from canon.instrumentation.events import _EVENTS_FILE, CanonEvent
from canon.instrumentation.models import (
    AnswerEvent,
    FunnelEvent,
    FunnelMilestone,
    ReconcileDecisionEvent,
)

if TYPE_CHECKING:
    from pathlib import Path

__all__ = [
    "BytesSummary",
    "EventReport",
    "FunnelReport",
    "LatencySummary",
    "build_funnel",
    "build_report",
    "read_events",
]

_ORDERED_MILESTONES = [
    FunnelMilestone.SETUP_STARTED,
    FunnelMilestone.CONNECTION_ADDED,
    FunnelMilestone.BOOTSTRAP_COMPLETED,
    FunnelMilestone.FIRST_ANSWER_SERVED,
    FunnelMilestone.FIRST_CURATED_REVIEW_COMPLETED,
]


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


class FunnelReport(BaseModel):
    """Onboarding funnel state derived from the local event log (OB-S6, SPEC-onboarding §9)."""

    model_config = ConfigDict(frozen=True)

    milestones: dict[str, str]
    """milestone value → first recorded timestamp (ISO string)."""
    time_to_first_answer_seconds: float | None
    """Seconds from setup_started to first_answer_served; None when either is absent."""
    reached: list[str]
    """Ordered list of milestone values that have been recorded."""
    dropped_after: str | None
    """Last milestone reached before the funnel stalls; None when all five are complete."""


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
        if kind == "funnel_milestone":
            return FunnelEvent.model_validate(data)
    except ValidationError:
        pass
    return None


@overload
def read_events(
    project_root: Path,
    last: int | None = ...,
    *,
    kind: Literal["served_answer"],
) -> list[AnswerEvent]: ...


@overload
def read_events(
    project_root: Path,
    last: int | None = ...,
    *,
    kind: Literal["reconcile_decision"],
) -> list[ReconcileDecisionEvent]: ...


@overload
def read_events(
    project_root: Path,
    last: int | None = ...,
    *,
    kind: Literal["funnel_milestone"],
) -> list[FunnelEvent]: ...


@overload
def read_events(
    project_root: Path,
    last: int | None = ...,
    *,
    kind: None = ...,
) -> list[AnswerEvent | ReconcileDecisionEvent | FunnelEvent]: ...


def read_events(
    project_root: Path,
    last: int | None = None,
    *,
    kind: Literal["served_answer", "reconcile_decision", "funnel_milestone"] | None = None,
) -> (
    list[AnswerEvent]
    | list[ReconcileDecisionEvent]
    | list[FunnelEvent]
    | list[AnswerEvent | ReconcileDecisionEvent | FunnelEvent]
):
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


def build_funnel(events: list[FunnelEvent]) -> FunnelReport:
    """Derive the onboarding funnel state from a list of FunnelEvents (OB-S6 AC2)."""
    first_ts: dict[str, str] = {}
    for event in events:
        key = str(event.milestone)
        if key not in first_ts:
            first_ts[key] = event.ts

    reached = [str(m) for m in _ORDERED_MILESTONES if str(m) in first_ts]

    ttfa: float | None = None
    started = first_ts.get(str(FunnelMilestone.SETUP_STARTED))
    answered = first_ts.get(str(FunnelMilestone.FIRST_ANSWER_SERVED))
    if started and answered:
        try:
            t0 = datetime.fromisoformat(started)
            t1 = datetime.fromisoformat(answered)
            if t0.tzinfo is None:
                t0 = t0.replace(tzinfo=UTC)
            if t1.tzinfo is None:
                t1 = t1.replace(tzinfo=UTC)
            ttfa = (t1 - t0).total_seconds()
        except ValueError:
            pass

    all_reached = len(reached) == len(_ORDERED_MILESTONES)
    dropped_after = reached[-1] if reached and not all_reached else None

    return FunnelReport(
        milestones=first_ts,
        time_to_first_answer_seconds=ttfa,
        reached=reached,
        dropped_after=dropped_after,
    )


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
