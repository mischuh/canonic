"""Opt-in aggregate telemetry payload (SPEC-E16 Part 2 §5).

No transport exists yet — sending requires a privacy review of this exact payload
(P1 spec §12 open question) that hasn't happened. This module defines precisely what
*would* be sent so the shape is reviewable and previewable (``canonic report
--telemetry-preview``) ahead of any sending code, rather than being a black box that
shows up only once telemetry is wired to a real endpoint.

Every field here is a count, distribution, or latency/accuracy aggregate — never a
query hash, resolved binding, SQL, ``compiled_sql_hash``, or freshness source name.
Those could re-identify a query or reveal warehouse/schema content, so they never
leave the local log, even under this payload.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from canonic.instrumentation.report import (
        CalibrationReport,
        CorrectionRecurrenceReport,
        EventReport,
        FunnelReport,
    )

__all__ = ["TELEMETRY_SCHEMA_VERSION", "build_telemetry_payload"]

TELEMETRY_SCHEMA_VERSION = "1"


def build_telemetry_payload(
    report: EventReport,
    calibration: CalibrationReport,
    recurrence: CorrectionRecurrenceReport,
    funnel: FunnelReport,
    *,
    accuracy: float | None = None,
    baseline_accuracy: float | None = None,
) -> dict[str, Any]:
    """Build the exact aggregate payload opt-in telemetry would send.

    ``accuracy``/``baseline_accuracy`` are omitted entirely (not sent as ``null``) when
    not supplied — accuracy-harness results aren't persisted to the local log yet, so
    ``canonic report``'s preview never fabricates a number for them.
    """
    payload: dict[str, Any] = {
        "schema_version": TELEMETRY_SCHEMA_VERSION,
        "answer_count": report.count,
        "error_distribution": dict(report.error_distribution),
        "latency": report.latency.model_dump(mode="json") if report.latency is not None else None,
        "bytes_scanned": report.bytes_scanned.model_dump(mode="json")
        if report.bytes_scanned is not None
        else None,
        "stale_answer_count": report.stale_answers,
        "guardrail_hit_count": report.guardrail_coverage,
        "trust_calibration": [b.model_dump(mode="json") for b in calibration.buckets],
        "recurring_binding_count": len(recurrence.entries),
        "funnel_reached": list(funnel.reached),
        "time_to_first_answer_seconds": funnel.time_to_first_answer_seconds,
    }
    if accuracy is not None:
        payload["accuracy"] = accuracy
    if baseline_accuracy is not None:
        payload["baseline_accuracy"] = baseline_accuracy
    return payload
