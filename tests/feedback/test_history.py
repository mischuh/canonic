"""Tests for BindingOutcomeHistory (SPEC-E11 §3-§5)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from canonic.feedback.history import BindingOutcomeHistory
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


def _ts(days_ago: float = 0) -> str:
    return (datetime.now(UTC) - timedelta(days=days_ago)).isoformat()


def _answer(**overrides: Any) -> AnswerEvent:
    return AnswerEvent.model_validate({**_BASE_ANSWER, **overrides})


def _outcome(**overrides: Any) -> AnswerOutcomeEvent:
    return AnswerOutcomeEvent.model_validate({**_BASE_OUTCOME, **overrides})


_BINDING = "orders.total_revenue"


def test_empty_history_has_no_bindings() -> None:
    history = BindingOutcomeHistory.from_events([], [])
    assert history.bindings() == []
    assert history.wrong_definition_count(_BINDING, window_days=90) == 0
    assert history.is_capped(_BINDING, window_days=90) is False


def test_joins_outcome_to_binding_via_answer_event() -> None:
    answers = [_answer()]
    outcomes = [_outcome(ts=_ts(1))]
    history = BindingOutcomeHistory.from_events(answers, outcomes)
    assert history.bindings() == [_BINDING]
    assert history.wrong_definition_count(_BINDING, window_days=90) == 1


def test_unmatched_ref_produces_no_record() -> None:
    history = BindingOutcomeHistory.from_events([], [_outcome(ref="sha256:missing")])
    assert history.bindings() == []


def test_dedup_by_ref_keeps_latest_verdict() -> None:
    """Reuses latest_outcome_by_ref — a re-marked answer counts once."""
    answers = [_answer()]
    outcomes = [
        _outcome(ts=_ts(2), verdict="incorrect", reason_code="wrong_definition"),
        _outcome(ts=_ts(1), verdict="correct", reason_code=None),  # later mark wins
    ]
    history = BindingOutcomeHistory.from_events(answers, outcomes)
    assert history.wrong_definition_count(_BINDING, window_days=90) == 0
    assert len(history.records_for(_BINDING)) == 1


class TestAttributionQuarantine:
    """S1: only wrong_definition counts toward the pattern gate / trust cap."""

    def test_wrong_data_never_counts(self) -> None:
        answers = [_answer(query_hash="sha256:1"), _answer(query_hash="sha256:2")]
        outcomes = [
            _outcome(ref="sha256:1", ts=_ts(1), reason_code="wrong_data"),
            _outcome(ref="sha256:2", ts=_ts(1), reason_code="wrong_data"),
        ]
        history = BindingOutcomeHistory.from_events(answers, outcomes)
        assert history.wrong_definition_count(_BINDING, window_days=90) == 0
        assert history.is_capped(_BINDING, window_days=90) is False
        # Still visible in the raw record set for the audit (§6).
        assert len(history.records_for(_BINDING)) == 2

    def test_wrong_interpretation_never_counts(self) -> None:
        answers = [_answer()]
        outcomes = [_outcome(ts=_ts(1), reason_code="wrong_interpretation")]
        history = BindingOutcomeHistory.from_events(answers, outcomes)
        assert history.wrong_definition_count(_BINDING, window_days=90) == 0

    def test_unspecified_never_counts(self) -> None:
        answers = [_answer()]
        outcomes = [_outcome(ts=_ts(1), reason_code="unspecified")]
        history = BindingOutcomeHistory.from_events(answers, outcomes)
        assert history.wrong_definition_count(_BINDING, window_days=90) == 0

    def test_correct_verdict_never_counts(self) -> None:
        answers = [_answer()]
        outcomes = [_outcome(ts=_ts(1), verdict="correct", reason_code=None)]
        history = BindingOutcomeHistory.from_events(answers, outcomes)
        assert history.wrong_definition_count(_BINDING, window_days=90) == 0


class TestPatternGateInputs:
    def test_count_within_window(self) -> None:
        answers = [_answer(query_hash="sha256:1"), _answer(query_hash="sha256:2")]
        outcomes = [
            _outcome(ref="sha256:1", ts=_ts(1)),
            _outcome(ref="sha256:2", ts=_ts(2)),
        ]
        history = BindingOutcomeHistory.from_events(answers, outcomes)
        assert history.wrong_definition_count(_BINDING, window_days=90) == 2

    def test_outcomes_outside_window_excluded(self) -> None:
        answers = [_answer(query_hash="sha256:1"), _answer(query_hash="sha256:2")]
        outcomes = [
            _outcome(ref="sha256:1", ts=_ts(1)),
            _outcome(ref="sha256:2", ts=_ts(200)),  # outside a 90-day window
        ]
        history = BindingOutcomeHistory.from_events(answers, outcomes)
        assert history.wrong_definition_count(_BINDING, window_days=90) == 1

    def test_distinct_markers_counts_unique_roles(self) -> None:
        answers = [_answer(query_hash="sha256:1"), _answer(query_hash="sha256:2")]
        outcomes = [
            _outcome(ref="sha256:1", ts=_ts(1), marked_by="analyst"),
            _outcome(ref="sha256:2", ts=_ts(1), marked_by="analyst"),
        ]
        history = BindingOutcomeHistory.from_events(answers, outcomes)
        assert history.distinct_markers(_BINDING, window_days=90) == 1

    def test_distinct_markers_counts_different_roles(self) -> None:
        answers = [_answer(query_hash="sha256:1"), _answer(query_hash="sha256:2")]
        outcomes = [
            _outcome(ref="sha256:1", ts=_ts(1), marked_by="analyst"),
            _outcome(ref="sha256:2", ts=_ts(1), marked_by="ci"),
        ]
        history = BindingOutcomeHistory.from_events(answers, outcomes)
        assert history.distinct_markers(_BINDING, window_days=90) == 2

    def test_wrong_definition_refs_sorted_and_deduped(self) -> None:
        answers = [_answer(query_hash="sha256:2"), _answer(query_hash="sha256:1")]
        outcomes = [
            _outcome(ref="sha256:2", ts=_ts(1)),
            _outcome(ref="sha256:1", ts=_ts(1)),
        ]
        history = BindingOutcomeHistory.from_events(answers, outcomes)
        assert history.wrong_definition_refs(_BINDING, window_days=90) == [
            "sha256:1",
            "sha256:2",
        ]


class TestTrustCapDecay:
    """S4/§9: recent confirmed-wrong caps; a later correct or an expired window lifts it."""

    def test_recent_wrong_definition_caps(self) -> None:
        answers = [_answer()]
        outcomes = [_outcome(ts=_ts(1))]
        history = BindingOutcomeHistory.from_events(answers, outcomes)
        assert history.is_capped(_BINDING, window_days=90) is True

    def test_wrong_definition_outside_window_does_not_cap(self) -> None:
        answers = [_answer()]
        outcomes = [_outcome(ts=_ts(200))]
        history = BindingOutcomeHistory.from_events(answers, outcomes)
        assert history.is_capped(_BINDING, window_days=90) is False

    def test_later_correct_outcome_lifts_the_cap(self) -> None:
        answers = [_answer(query_hash="sha256:1"), _answer(query_hash="sha256:2")]
        outcomes = [
            _outcome(
                ref="sha256:1", ts=_ts(5), verdict="incorrect", reason_code="wrong_definition"
            ),
            _outcome(ref="sha256:2", ts=_ts(1), verdict="correct", reason_code=None),
        ]
        history = BindingOutcomeHistory.from_events(answers, outcomes)
        assert history.is_capped(_BINDING, window_days=90) is False

    def test_earlier_correct_outcome_does_not_lift_the_cap(self) -> None:
        answers = [_answer(query_hash="sha256:1"), _answer(query_hash="sha256:2")]
        outcomes = [
            _outcome(ref="sha256:1", ts=_ts(5), verdict="correct", reason_code=None),
            _outcome(
                ref="sha256:2", ts=_ts(1), verdict="incorrect", reason_code="wrong_definition"
            ),
        ]
        history = BindingOutcomeHistory.from_events(answers, outcomes)
        assert history.is_capped(_BINDING, window_days=90) is True
