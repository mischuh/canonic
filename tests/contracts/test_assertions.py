"""Unit tests for the assertion matcher (SPEC-Fuller-E15 §3) — pure, execution-free."""

from __future__ import annotations

from decimal import Decimal

import pytest

from canonic.compiler.query import SemanticQuery
from canonic.connectors.base import ResultColumn, ResultSet
from canonic.contracts.assertions import (
    AssertionOutcome,
    accuracy_report,
    assertion_metrics,
    assertion_to_query,
    is_executable,
    match_result,
)
from canonic.contracts.models import Assertion, AssertionExpect
from canonic.exc import ValidationFailed


def _assertion(**expect: object) -> Assertion:
    return Assertion(
        id="revenue-2025-q1",
        query={"metrics": ["revenue"], "filters": ["order_date in 2025-Q1"]},
        expect=AssertionExpect(**expect),  # type: ignore[arg-type]
        source_of_truth="Finance close, FY25 Q1",
    )


def _result(value: object, *, column: str = "revenue") -> ResultSet:
    return ResultSet(columns=[ResultColumn(name=column, type="decimal")], rows=[[value]])


class TestIsExecutable:
    def test_semantic_query_form_is_executable(self) -> None:
        assert is_executable(_assertion(values={"revenue": 1.0}))

    def test_native_candidate_form_is_not_executable(self) -> None:
        candidate = Assertion(
            id="usage-x", query={"native": "sum(amount)", "references": ["orders.amount"]}
        )
        assert not is_executable(candidate)

    def test_empty_metrics_is_not_executable(self) -> None:
        assert not is_executable(Assertion(id="x", query={"metrics": []}))


class TestAssertionToQuery:
    def test_builds_semantic_query(self) -> None:
        sq = assertion_to_query(_assertion(values={"revenue": 1.0}))
        assert isinstance(sq, SemanticQuery)
        assert sq.metrics == ["revenue"]
        assert sq.filters == ["order_date in 2025-Q1"]

    def test_non_executable_raises_validation_failed(self) -> None:
        candidate = Assertion(id="x", query={"native": "sum(amount)"})
        with pytest.raises(ValidationFailed, match="executable semantic query"):
            assertion_to_query(candidate)


class TestMatchResult:
    def test_exact_scalar_match_passes(self) -> None:
        outcome = match_result(
            _assertion(rows=1, values={"revenue": 100.0}), _result(Decimal("100.0"))
        )
        assert outcome == AssertionOutcome(assertion_id="revenue-2025-q1", passed=True)

    def test_scalar_mismatch_fails_with_diff(self) -> None:
        outcome = match_result(_assertion(values={"revenue": 4218334.10}), _result(4100000))
        assert not outcome.passed
        assert "expected {revenue: 4218334.1}" in outcome.detail
        assert "got {revenue: 4100000}" in outcome.detail

    def test_ac3_relative_tolerance_within_one_percent_passes(self) -> None:
        # 99.5 is within 1% of 100.0
        outcome = match_result(_assertion(values={"revenue": 100.0}, tolerance=0.01), _result(99.5))
        assert outcome.passed

    def test_ac3_relative_tolerance_outside_one_percent_fails(self) -> None:
        # 98.0 is 2% off — outside a 1% tolerance
        outcome = match_result(_assertion(values={"revenue": 100.0}, tolerance=0.01), _result(98.0))
        assert not outcome.passed

    def test_default_is_exact_match(self) -> None:
        outcome = match_result(_assertion(values={"revenue": 100.0}), _result(100.01))
        assert not outcome.passed

    def test_row_count_mismatch_fails(self) -> None:
        rs = ResultSet(columns=[ResultColumn(name="revenue", type="decimal")], rows=[[1], [2]])
        outcome = match_result(_assertion(rows=1), rs)
        assert not outcome.passed
        assert "expected 1 row(s), got 2" in outcome.detail

    def test_missing_expected_column_fails(self) -> None:
        outcome = match_result(_assertion(values={"profit": 1.0}), _result(1.0, column="revenue"))
        assert not outcome.passed
        assert "profit" in outcome.detail

    def test_no_rows_for_expected_value_fails(self) -> None:
        rs = ResultSet(columns=[ResultColumn(name="revenue", type="decimal")], rows=[])
        outcome = match_result(_assertion(values={"revenue": 1.0}), rs)
        assert not outcome.passed
        assert "no rows" in outcome.detail

    def test_zero_expected_uses_absolute_fallback(self) -> None:
        outcome = match_result(_assertion(values={"revenue": 0.0}, tolerance=0.01), _result(0.005))
        assert outcome.passed

    def test_metric_name_resolves_to_measure_column(self) -> None:
        # Query references metric "revenue"; the SQL column is the measure "total_revenue".
        rs = _result(Decimal("100.0"), column="total_revenue")
        outcome = match_result(
            _assertion(values={"revenue": 100.0}),
            rs,
            resolved={"revenue": "orders.total_revenue"},
        )
        assert outcome.passed


class TestAccuracyReport:
    """Aggregation of per-assertion outcomes into the harness accuracy number (§3.4)."""

    def test_accuracy_is_passed_over_total(self) -> None:
        report = accuracy_report(
            [
                AssertionOutcome("a", passed=True),
                AssertionOutcome("b", passed=True),
                AssertionOutcome("c", passed=False, detail="c: diverged"),
                AssertionOutcome("d", passed=True),
            ]
        )
        assert report.total == 4
        assert report.passed == 3
        assert report.accuracy == 0.75

    def test_all_passing_is_full_accuracy(self) -> None:
        report = accuracy_report([AssertionOutcome("a", passed=True)])
        assert report.accuracy == 1.0
        assert report.failures == ()

    def test_empty_set_is_vacuously_full_accuracy(self) -> None:
        report = accuracy_report([])
        assert report.total == 0
        assert report.accuracy == 1.0

    def test_failures_preserve_input_order(self) -> None:
        report = accuracy_report(
            [
                AssertionOutcome("a", passed=False, detail="a bad"),
                AssertionOutcome("b", passed=True),
                AssertionOutcome("c", passed=False, detail="c bad"),
            ]
        )
        assert [o.assertion_id for o in report.failures] == ["a", "c"]

    def test_to_dict_is_json_native(self) -> None:
        report = accuracy_report(
            [
                AssertionOutcome("a", passed=True),
                AssertionOutcome("b", passed=False, detail="b: diverged"),
            ]
        )
        assert report.to_dict() == {
            "accuracy": 0.5,
            "passed": 1,
            "total": 2,
            "failures": [{"assertion_id": "b", "detail": "b: diverged"}],
        }


class TestAssertionMetrics:
    def test_returns_metric_names(self) -> None:
        assert assertion_metrics(_assertion(values={"revenue": 1.0})) == ["revenue"]

    def test_no_metrics_returns_empty(self) -> None:
        assert assertion_metrics(Assertion(id="x", query={"native": "x"})) == []
