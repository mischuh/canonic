"""Tests for build_telemetry_payload — content-safety of the opt-in telemetry shape
(SPEC-E16 Part 2 §5, S4-AC2).
"""

from __future__ import annotations

import json

from canonic.instrumentation.models import AnswerEvent
from canonic.instrumentation.report import (
    build_calibration,
    build_correction_recurrence,
    build_funnel,
    build_report,
)
from canonic.instrumentation.telemetry import TELEMETRY_SCHEMA_VERSION, build_telemetry_payload

_FORBIDDEN_KEYS = {
    "query_hash",
    "resolved",
    "sql",
    "compiled_sql_hash",
    "source",
    "connection",
}


def _answer(**overrides: object) -> AnswerEvent:
    base = {
        "ts": "2026-01-01T00:00:00+00:00",
        "kind": "served_answer",
        "contract_schema": "2.2",
        "query_hash": "sha256:super-secret-query-hash",
        "compiled_sql_hash": "sha256:super-secret-sql-hash",
        "connection": "warehouse_pg",
        "resolved": {"metrics": {"revenue": "orders.total_revenue"}},
        "guardrails_fired": ["revenue-excludes-refunds"],
        "finality": None,
        "freshness": [{"source": "orders", "stale": False, "age_days": 1}],
        "latency_ms": 120,
        "bytes_scanned": 2048,
        "error": None,
        "trust_score": "caution",
        "cache_hit": None,
        "over_limit_blocked": None,
    }
    return AnswerEvent.model_validate({**base, **overrides})


def test_schema_version_present() -> None:
    rep = build_report([])
    funnel = build_funnel([])
    calibration = build_calibration([], [])
    recurrence = build_correction_recurrence([], [])
    payload = build_telemetry_payload(rep, calibration, recurrence, funnel)
    assert payload["schema_version"] == TELEMETRY_SCHEMA_VERSION


def test_payload_never_contains_query_hash_or_sql_or_bindings() -> None:
    answers = [_answer()]
    rep = build_report(answers)
    funnel = build_funnel([])
    calibration = build_calibration(answers, [])
    recurrence = build_correction_recurrence(answers, [])
    payload = build_telemetry_payload(rep, calibration, recurrence, funnel)

    dumped = json.dumps(payload)
    assert "super-secret-query-hash" not in dumped
    assert "super-secret-sql-hash" not in dumped
    assert "orders.total_revenue" not in dumped
    assert "orders" not in dumped
    assert "warehouse_pg" not in dumped

    def _walk_keys(obj: object) -> set[str]:
        keys: set[str] = set()
        if isinstance(obj, dict):
            for k, v in obj.items():
                keys.add(k)
                keys |= _walk_keys(v)
        elif isinstance(obj, list):
            for item in obj:
                keys |= _walk_keys(item)
        return keys

    assert _walk_keys(payload) & _FORBIDDEN_KEYS == set()


def test_accuracy_omitted_when_not_supplied() -> None:
    rep = build_report([])
    funnel = build_funnel([])
    calibration = build_calibration([], [])
    recurrence = build_correction_recurrence([], [])
    payload = build_telemetry_payload(rep, calibration, recurrence, funnel)
    assert "accuracy" not in payload
    assert "baseline_accuracy" not in payload


def test_accuracy_included_when_supplied() -> None:
    rep = build_report([])
    funnel = build_funnel([])
    calibration = build_calibration([], [])
    recurrence = build_correction_recurrence([], [])
    payload = build_telemetry_payload(
        rep, calibration, recurrence, funnel, accuracy=0.95, baseline_accuracy=0.5
    )
    assert payload["accuracy"] == 0.95
    assert payload["baseline_accuracy"] == 0.5


def test_recurring_binding_count_not_names() -> None:
    """Recurrence is a count in telemetry — binding names never leave the log (§5)."""
    answers = [
        _answer(query_hash="sha256:1", resolved={"metrics": {"revenue": "orders.total_revenue"}}),
        _answer(query_hash="sha256:2", resolved={"metrics": {"revenue": "orders.total_revenue"}}),
    ]
    from canonic.instrumentation.models import AnswerOutcomeEvent

    outcomes = [
        AnswerOutcomeEvent.model_validate(
            {
                "ts": "2026-01-01T00:01:00+00:00",
                "kind": "answer_outcome",
                "ref": "sha256:1",
                "verdict": "incorrect",
                "marked_by": "analyst",
            }
        ),
        AnswerOutcomeEvent.model_validate(
            {
                "ts": "2026-01-01T00:02:00+00:00",
                "kind": "answer_outcome",
                "ref": "sha256:2",
                "verdict": "incorrect",
                "marked_by": "analyst",
            }
        ),
    ]
    rep = build_report(answers)
    funnel = build_funnel([])
    calibration = build_calibration(answers, outcomes)
    recurrence = build_correction_recurrence(answers, outcomes)
    assert recurrence.entries  # sanity: recurrence was actually detected
    payload = build_telemetry_payload(rep, calibration, recurrence, funnel)
    assert payload["recurring_binding_count"] == 1
    assert "orders.total_revenue" not in json.dumps(payload)
