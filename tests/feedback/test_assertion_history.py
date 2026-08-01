"""Tests for AssertionHistory / write_assertion_results (SPEC-E14 §5, "+ E16 Phase 2")."""

from __future__ import annotations

from typing import TYPE_CHECKING

from canonic.contracts.assertions import AssertionOutcome
from canonic.feedback.assertion_history import AssertionHistory, write_assertion_results

if TYPE_CHECKING:
    from pathlib import Path

_REVENUE_BINDING = "orders.total_revenue"
_UNITS_BINDING = "order_items.units_sold"


def test_empty_history_has_no_record(tmp_path: Path) -> None:
    history = AssertionHistory.from_project(tmp_path)
    assert history.status_for(_REVENUE_BINDING) is None


def test_missing_file_yields_empty_history(tmp_path: Path) -> None:
    history = AssertionHistory.from_project(tmp_path)
    assert history.status_for(_REVENUE_BINDING) is None


def test_malformed_json_yields_empty_history(tmp_path: Path) -> None:
    (tmp_path / ".canonic").mkdir()
    (tmp_path / ".canonic" / "assertions.json").write_text("{not valid json")
    history = AssertionHistory.from_project(tmp_path)
    assert history.status_for(_REVENUE_BINDING) is None


def test_write_then_read_round_trips_a_passing_binding(tmp_path: Path) -> None:
    outcomes = [
        AssertionOutcome(assertion_id="revenue-q1", passed=True, bindings=(_REVENUE_BINDING,))
    ]
    write_assertion_results(tmp_path, outcomes)
    history = AssertionHistory.from_project(tmp_path)
    record = history.status_for(_REVENUE_BINDING)
    assert record is not None
    assert record.passed is True
    assert record.assertion_id == "revenue-q1"


def test_write_then_read_round_trips_a_failing_binding(tmp_path: Path) -> None:
    outcomes = [
        AssertionOutcome(
            assertion_id="revenue-q1",
            passed=False,
            detail="mismatch",
            bindings=(_REVENUE_BINDING,),
        )
    ]
    write_assertion_results(tmp_path, outcomes)
    history = AssertionHistory.from_project(tmp_path)
    record = history.status_for(_REVENUE_BINDING)
    assert record is not None
    assert record.passed is False
    assert record.assertion_id == "revenue-q1"


def test_second_run_overwrites_bindings_no_longer_covered(tmp_path: Path) -> None:
    """A binding dropped from the current assertion set reverts to unrecorded, not stale."""
    write_assertion_results(
        tmp_path,
        [AssertionOutcome(assertion_id="units-q1", passed=True, bindings=(_UNITS_BINDING,))],
    )
    assert AssertionHistory.from_project(tmp_path).status_for(_UNITS_BINDING) is not None

    write_assertion_results(
        tmp_path,
        [AssertionOutcome(assertion_id="revenue-q1", passed=True, bindings=(_REVENUE_BINDING,))],
    )
    history = AssertionHistory.from_project(tmp_path)
    assert history.status_for(_UNITS_BINDING) is None
    assert history.status_for(_REVENUE_BINDING) is not None


def test_two_assertions_sharing_a_binding_merge_worst_wins(tmp_path: Path) -> None:
    outcomes = [
        AssertionOutcome(assertion_id="revenue-q1", passed=True, bindings=(_REVENUE_BINDING,)),
        AssertionOutcome(
            assertion_id="revenue-q2", passed=False, detail="x", bindings=(_REVENUE_BINDING,)
        ),
    ]
    write_assertion_results(tmp_path, outcomes)
    record = AssertionHistory.from_project(tmp_path).status_for(_REVENUE_BINDING)
    assert record is not None
    assert record.passed is False
    assert record.assertion_id == "revenue-q2"


def test_outcome_with_no_bindings_writes_nothing(tmp_path: Path) -> None:
    outcomes = [AssertionOutcome(assertion_id="candidate", passed=True)]
    write_assertion_results(tmp_path, outcomes)
    history = AssertionHistory.from_project(tmp_path)
    assert history.status_for(_REVENUE_BINDING) is None
