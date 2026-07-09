"""Tests for build_feedback_report — the E11 learned-change audit (SPEC-E11 §6, S5-AC1)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from canonic.config import FeedbackConfig
from canonic.feedback.history import BindingOutcomeHistory
from canonic.feedback.report import build_feedback_report
from canonic.instrumentation.models import AnswerEvent, AnswerOutcomeEvent

_BASE_ANSWER: dict[str, Any] = {
    "ts": "2026-01-01T00:00:00+00:00",
    "kind": "served_answer",
    "contract_schema": "2.2",
    "query_hash": "sha256:aaa",
    "compiled_sql_hash": "sha256:bbb",
    "connection": "wh",
    "resolved": {"metrics": {"revenue": "orders.total_revenue"}},
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

_BASE_OUTCOME: dict[str, Any] = {
    "ts": "2026-01-01T00:01:00+00:00",
    "kind": "answer_outcome",
    "ref": "sha256:aaa",
    "verdict": "incorrect",
    "reason_code": "wrong_definition",
    "correction": None,
    "marked_by": "analyst",
}


def _ts(days_ago: float = 1) -> str:
    return (datetime.now(UTC) - timedelta(days=days_ago)).isoformat()


def _answer(**overrides: Any) -> AnswerEvent:
    return AnswerEvent.model_validate({**_BASE_ANSWER, **overrides})


def _outcome(**overrides: Any) -> AnswerOutcomeEvent:
    return AnswerOutcomeEvent.model_validate({**_BASE_OUTCOME, **overrides})


def test_empty_history_yields_empty_report() -> None:
    history = BindingOutcomeHistory.from_events([], [])
    report = build_feedback_report(history, FeedbackConfig())
    assert report.entries == []


def test_single_incident_visible_but_not_gated() -> None:
    """S2-AC1: a single outcome shows up in the audit even though it never reaches E4."""
    answers = [_answer()]
    outcomes = [_outcome(ts=_ts(1))]
    history = BindingOutcomeHistory.from_events(answers, outcomes)
    report = build_feedback_report(history, FeedbackConfig())
    assert len(report.entries) == 1
    entry = report.entries[0]
    assert entry.binding == "orders.total_revenue"
    assert entry.wrong_definition_count == 1
    assert entry.gated is False


def test_recurring_pattern_gated_and_capped() -> None:
    answers = [_answer(query_hash="sha256:1"), _answer(query_hash="sha256:2")]
    outcomes = [
        _outcome(ref="sha256:1", ts=_ts(2), marked_by="analyst"),
        _outcome(ref="sha256:2", ts=_ts(1), marked_by="ci"),
    ]
    history = BindingOutcomeHistory.from_events(answers, outcomes)
    report = build_feedback_report(history, FeedbackConfig())
    entry = report.entries[0]
    assert entry.wrong_definition_count == 2
    assert entry.distinct_markers == 2
    assert entry.gated is True
    assert entry.capped is True
    assert entry.refs == ["sha256:1", "sha256:2"]


def test_wrong_data_only_binding_excluded() -> None:
    answers = [_answer()]
    outcomes = [_outcome(ts=_ts(1), reason_code="wrong_data")]
    history = BindingOutcomeHistory.from_events(answers, outcomes)
    report = build_feedback_report(history, FeedbackConfig())
    assert report.entries == []


def test_sorted_by_count_desc_then_binding_asc() -> None:
    answers = [
        _answer(query_hash="sha256:1", resolved={"metrics": {"a": "orders.a"}}),
        _answer(query_hash="sha256:2", resolved={"metrics": {"b": "orders.b"}}),
        _answer(query_hash="sha256:3", resolved={"metrics": {"b": "orders.b"}}),
    ]
    outcomes = [_outcome(ref=a.query_hash, ts=_ts(1)) for a in answers]
    history = BindingOutcomeHistory.from_events(answers, outcomes)
    report = build_feedback_report(history, FeedbackConfig())
    assert [e.binding for e in report.entries] == ["orders.b", "orders.a"]
