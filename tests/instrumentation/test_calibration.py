"""Tests for build_calibration and build_correction_recurrence (SPEC-E16 Part 2 §4)."""

from __future__ import annotations

from typing import Any

from canonic.instrumentation.models import AnswerEvent, AnswerOutcomeEvent
from canonic.instrumentation.report import build_calibration, build_correction_recurrence

_BASE_ANSWER: dict[str, Any] = {
    "ts": "2026-01-01T00:00:00+00:00",
    "kind": "served_answer",
    "contract_schema": "2.2",
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

_BASE_OUTCOME: dict[str, Any] = {
    "ts": "2026-01-01T00:01:00+00:00",
    "kind": "answer_outcome",
    "ref": "sha256:aaa",
    "verdict": "correct",
    "reason_code": None,
    "correction": None,
    "marked_by": "analyst",
}


def _answer(**overrides: Any) -> AnswerEvent:
    return AnswerEvent.model_validate({**_BASE_ANSWER, **overrides})


def _outcome(**overrides: Any) -> AnswerOutcomeEvent:
    return AnswerOutcomeEvent.model_validate({**_BASE_OUTCOME, **overrides})


# ---------------------------------------------------------------------------
# build_calibration
# ---------------------------------------------------------------------------


def test_empty_inputs_yield_empty_report() -> None:
    report = build_calibration([], [])
    assert report.buckets == []
    assert report.unmatched == 0


def test_caution_predicts_incorrect_more_than_trusted() -> None:
    answers = [
        _answer(query_hash="sha256:1", trust_score="caution"),
        _answer(query_hash="sha256:2", trust_score="trusted"),
    ]
    outcomes = [
        _outcome(ref="sha256:1", verdict="incorrect"),
        _outcome(ref="sha256:2", verdict="correct"),
    ]
    report = build_calibration(answers, outcomes)
    by_tier = {b.tier: b for b in report.buckets}
    assert by_tier["caution"].incorrect_rate == 1.0
    assert by_tier["trusted"].incorrect_rate == 0.0


def test_buckets_ordered_worst_to_best() -> None:
    answers = [
        _answer(query_hash="sha256:1", trust_score="trusted"),
        _answer(query_hash="sha256:2", trust_score="caution"),
        _answer(query_hash="sha256:3", trust_score="provisional"),
    ]
    outcomes = [_outcome(ref=a.query_hash, verdict="correct") for a in answers]
    report = build_calibration(answers, outcomes)
    assert [b.tier for b in report.buckets] == ["caution", "provisional", "trusted"]


def test_unmatched_ref_counted_separately() -> None:
    report = build_calibration([], [_outcome(ref="sha256:missing")])
    assert report.buckets == []
    assert report.unmatched == 1


def test_answer_without_trust_score_counted_unmatched() -> None:
    answers = [_answer(trust_score=None)]
    outcomes = [_outcome(ref="sha256:aaa")]
    report = build_calibration(answers, outcomes)
    assert report.buckets == []
    assert report.unmatched == 1


def test_dedup_by_ref_counts_once() -> None:
    """S9 open question: same answer marked twice is counted once for calibration."""
    answers = [_answer(trust_score="trusted")]
    outcomes = [
        _outcome(ts="2026-01-01T00:01:00+00:00", verdict="incorrect"),
        _outcome(ts="2026-01-01T00:02:00+00:00", verdict="correct"),  # later mark wins
    ]
    report = build_calibration(answers, outcomes)
    assert report.buckets[0].total == 1
    assert report.buckets[0].incorrect == 0


# ---------------------------------------------------------------------------
# build_correction_recurrence
# ---------------------------------------------------------------------------


def test_no_recurrence_below_threshold() -> None:
    answers = [_answer(resolved={"metrics": {"revenue": "orders.total_revenue"}})]
    outcomes = [_outcome(verdict="incorrect")]
    report = build_correction_recurrence(answers, outcomes)
    assert report.entries == []  # a single incorrect mark is not "recurring"


def test_recurring_binding_surfaces() -> None:
    answers = [
        _answer(query_hash="sha256:1", resolved={"metrics": {"revenue": "orders.total_revenue"}}),
        _answer(query_hash="sha256:2", resolved={"metrics": {"revenue": "orders.total_revenue"}}),
    ]
    outcomes = [
        _outcome(ref="sha256:1", verdict="incorrect"),
        _outcome(ref="sha256:2", verdict="incorrect"),
    ]
    report = build_correction_recurrence(answers, outcomes)
    assert len(report.entries) == 1
    assert report.entries[0].binding == "orders.total_revenue"
    assert report.entries[0].count == 2


def test_correct_outcomes_do_not_count() -> None:
    answers = [
        _answer(query_hash="sha256:1", resolved={"metrics": {"revenue": "orders.total_revenue"}}),
        _answer(query_hash="sha256:2", resolved={"metrics": {"revenue": "orders.total_revenue"}}),
    ]
    outcomes = [
        _outcome(ref="sha256:1", verdict="correct"),
        _outcome(ref="sha256:2", verdict="incorrect"),
    ]
    report = build_correction_recurrence(answers, outcomes)
    assert report.entries == []


def test_sorted_by_count_desc_then_binding_asc() -> None:
    answers = [
        _answer(query_hash="sha256:1", resolved={"metrics": {"a": "orders.a"}}),
        _answer(query_hash="sha256:2", resolved={"metrics": {"a": "orders.a"}}),
        _answer(query_hash="sha256:3", resolved={"metrics": {"b": "orders.b"}}),
        _answer(query_hash="sha256:4", resolved={"metrics": {"b": "orders.b"}}),
        _answer(query_hash="sha256:5", resolved={"metrics": {"b": "orders.b"}}),
    ]
    outcomes = [_outcome(ref=a.query_hash, verdict="incorrect") for a in answers]
    report = build_correction_recurrence(answers, outcomes)
    assert [e.binding for e in report.entries] == ["orders.b", "orders.a"]
    assert [e.count for e in report.entries] == [3, 2]
