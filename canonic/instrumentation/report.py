"""Read path for the local event log — aggregates served-answer records (SPEC-E16 §4, §11 S4/S6)."""

from __future__ import annotations

import json
import math
from datetime import UTC, datetime
from json import JSONDecodeError
from typing import TYPE_CHECKING, Literal, overload

from pydantic import BaseModel, ConfigDict, ValidationError

from canonic.config import LOCAL_STATE_DIR
from canonic.instrumentation.events import _EVENTS_FILE, CanonicEvent
from canonic.instrumentation.models import (
    AnswerEvent,
    AnswerOutcomeEvent,
    FunnelEvent,
    FunnelMilestone,
    OutcomeVerdict,
    ReconcileDecisionEvent,
)
from canonic.trust.models import TrustTier

if TYPE_CHECKING:
    from pathlib import Path

__all__ = [
    "BytesSummary",
    "CalibrationBucket",
    "CalibrationReport",
    "CorrectionRecurrenceReport",
    "EventReport",
    "FunnelReport",
    "LatencySummary",
    "RecurrenceEntry",
    "build_calibration",
    "build_correction_recurrence",
    "build_funnel",
    "build_report",
    "latest_outcome_by_ref",
    "read_events",
]

_TIER_ORDER = list(TrustTier)

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


class CalibrationBucket(BaseModel):
    """Outcome verdicts for one E14 trust tier (SPEC-E16 Part 2 §4, S3-AC1)."""

    model_config = ConfigDict(frozen=True)

    tier: str
    total: int
    incorrect: int
    incorrect_rate: float


class CalibrationReport(BaseModel):
    """Whether ``caution`` predicts ``incorrect`` more than ``trusted`` (SPEC-E16 Part 2 §4)."""

    model_config = ConfigDict(frozen=True)

    buckets: list[CalibrationBucket]
    unmatched: int
    """``answer_outcome`` events whose ``ref`` matched no known AnswerEvent (excluded above)."""


class RecurrenceEntry(BaseModel):
    """A binding with more than one ``incorrect`` outcome (SPEC-E16 Part 2 §4)."""

    model_config = ConfigDict(frozen=True)

    binding: str
    """The resolved ``source.measure`` repeatedly marked incorrect."""
    count: int


class CorrectionRecurrenceReport(BaseModel):
    """Repeated ``incorrect`` outcomes on the same binding — a rising count means the
    feedback loop (E11) isn't closing (SPEC-E16 Part 2 §4).
    """

    model_config = ConfigDict(frozen=True)

    entries: list[RecurrenceEntry]
    """Sorted by count descending, then binding name — bindings with count == 1 excluded."""


def latest_outcome_by_ref(outcomes: list[AnswerOutcomeEvent]) -> dict[str, AnswerOutcomeEvent]:
    """Dedup outcomes by ``ref`` — the last recorded verdict per answer wins.

    A re-marked answer is counted once (SPEC-E16 Part 2 §9 open question: "counted once
    for calibration"). Outcomes are in file/append order, so later entries overwrite
    earlier ones for the same ``ref``. Shared with
    :class:`canonic.feedback.history.BindingOutcomeHistory` (SPEC-E11) so the two never
    drift on what "the latest outcome" means.
    """
    latest: dict[str, AnswerOutcomeEvent] = {}
    for outcome in outcomes:
        latest[outcome.ref] = outcome
    return latest


def _percentile(sorted_values: list[int], p: float) -> int:
    """Nearest-rank percentile on a pre-sorted list (must be non-empty)."""
    n = len(sorted_values)
    rank = math.ceil(p / 100.0 * n)
    return sorted_values[rank - 1]


def _parse_line(line: str) -> CanonicEvent | None:
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
        if kind == "answer_outcome":
            return AnswerOutcomeEvent.model_validate(data)
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
    kind: Literal["answer_outcome"],
) -> list[AnswerOutcomeEvent]: ...


@overload
def read_events(
    project_root: Path,
    last: int | None = ...,
    *,
    kind: None = ...,
) -> list[AnswerEvent | ReconcileDecisionEvent | FunnelEvent | AnswerOutcomeEvent]: ...


def read_events(
    project_root: Path,
    last: int | None = None,
    *,
    kind: Literal["served_answer", "reconcile_decision", "funnel_milestone", "answer_outcome"]
    | None = None,
) -> (
    list[AnswerEvent]
    | list[ReconcileDecisionEvent]
    | list[FunnelEvent]
    | list[AnswerOutcomeEvent]
    | list[AnswerEvent | ReconcileDecisionEvent | FunnelEvent | AnswerOutcomeEvent]
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

    events: list[CanonicEvent] = []
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


def build_calibration(
    answers: list[AnswerEvent], outcomes: list[AnswerOutcomeEvent]
) -> CalibrationReport:
    """Correlate E14 trust tiers with outcome verdicts (SPEC-E16 Part 2 §4, S3-AC1).

    Joins each outcome to its originating :class:`AnswerEvent` by ``ref == query_hash`` and
    buckets by the answer's ``trust_score`` tier, so ``canonic report`` can show whether
    ``caution`` predicts ``incorrect`` materially more than ``trusted`` — the metric that
    validates E14's predictiveness. Outcomes are deduped by ``ref`` first (one verdict per
    answer); outcomes whose ``ref`` matches no known answer, or whose answer has no trust
    score recorded, are counted in ``unmatched`` rather than a tier bucket.
    """
    by_hash = {a.query_hash: a for a in answers}
    tier_totals: dict[str, int] = {}
    tier_incorrect: dict[str, int] = {}
    unmatched = 0
    for ref, outcome in latest_outcome_by_ref(outcomes).items():
        answer = by_hash.get(ref)
        if answer is None or answer.trust_score is None:
            unmatched += 1
            continue
        tier_totals[answer.trust_score] = tier_totals.get(answer.trust_score, 0) + 1
        if outcome.verdict == OutcomeVerdict.INCORRECT:
            tier_incorrect[answer.trust_score] = tier_incorrect.get(answer.trust_score, 0) + 1

    buckets = [
        CalibrationBucket(
            tier=tier,
            total=total,
            incorrect=tier_incorrect.get(tier, 0),
            incorrect_rate=tier_incorrect.get(tier, 0) / total,
        )
        for tier, total in sorted(
            tier_totals.items(), key=lambda item: _TIER_ORDER.index(TrustTier(item[0]))
        )
    ]
    return CalibrationReport(buckets=buckets, unmatched=unmatched)


def build_correction_recurrence(
    answers: list[AnswerEvent], outcomes: list[AnswerOutcomeEvent]
) -> CorrectionRecurrenceReport:
    """Repeated ``incorrect`` outcomes on the same binding (SPEC-E16 Part 2 §4).

    A rising count for one binding is the signal that E11's feedback loop isn't closing —
    the same canonical definition keeps getting marked wrong. Deduped by ``ref`` like
    :func:`build_calibration`, so a re-marked answer counts once.
    """
    by_hash = {a.query_hash: a for a in answers}
    counts: dict[str, int] = {}
    for ref, outcome in latest_outcome_by_ref(outcomes).items():
        if outcome.verdict != OutcomeVerdict.INCORRECT:
            continue
        answer = by_hash.get(ref)
        if answer is None:
            continue
        for binding in answer.resolved.get("metrics", {}).values():
            counts[binding] = counts.get(binding, 0) + 1

    entries = sorted(
        (
            RecurrenceEntry(binding=binding, count=count)
            for binding, count in counts.items()
            if count > 1
        ),
        key=lambda e: (-e.count, e.binding),
    )
    return CorrectionRecurrenceReport(entries=entries)
