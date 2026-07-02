"""Assertion evaluation — trusted query → expected-result checks (SPEC-Fuller-E15 §3).

An assertion pairs a *semantic query* (names, not tables — so it survives compiler
changes) with the result it must produce. The comparison logic here is pure and
deterministic; execution (compile → run read-only) lives in the core service layer
so the compiler stays execution-free. The same matcher backs both the CI gate (a
mismatch raises ``ASSERTION_FAILED``) and E16's accuracy harness (counting passes).
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from canonic.compiler.query import SemanticQuery
    from canonic.connectors.base import ResultSet
    from canonic.contracts.models import Assertion, AssertionExpect

__all__ = [
    "AccuracyReport",
    "AssertionOutcome",
    "accuracy_report",
    "assertion_to_query",
    "is_executable",
    "match_result",
]


@dataclass(frozen=True, slots=True)
class AssertionOutcome:
    """The result of evaluating one assertion against an executed query result.

    ``detail`` is empty when ``passed`` is ``True``; otherwise it is a human-readable
    diff (the message carried by ``ASSERTION_FAILED``).
    """

    assertion_id: str
    passed: bool
    detail: str = ""


@dataclass(frozen=True, slots=True)
class AccuracyReport:
    """The aggregate of running a labeled assertion set — E16's accuracy number (§3.4).

    Assertions are the oracle: each executed assertion is one labeled question, and
    ``accuracy = passed / total`` turns ">90% accuracy" from aspirational to measured.
    Outcomes preserve input (load) order so the same assertion set yields the same number
    every run, which is what makes accuracy regressions detectable in CI.
    """

    outcomes: tuple[AssertionOutcome, ...]

    @property
    def total(self) -> int:
        """How many executable assertions were run (the harness denominator)."""
        return len(self.outcomes)

    @property
    def passed(self) -> int:
        """How many assertions matched their expectation within tolerance."""
        return sum(1 for o in self.outcomes if o.passed)

    @property
    def failures(self) -> tuple[AssertionOutcome, ...]:
        """The diverging assertions, in load order (each carries its diff in ``detail``)."""
        return tuple(o for o in self.outcomes if not o.passed)

    @property
    def accuracy(self) -> float:
        """``passed / total`` — vacuously ``1.0`` when no assertions ran (nothing to regress)."""
        return self.passed / self.total if self.total else 1.0

    def to_dict(self) -> dict[str, Any]:
        """A JSON-native summary for ``--json`` output and durable harness records."""
        return {
            "accuracy": self.accuracy,
            "passed": self.passed,
            "total": self.total,
            "failures": [
                {"assertion_id": o.assertion_id, "detail": o.detail} for o in self.failures
            ],
        }


def accuracy_report(outcomes: list[AssertionOutcome]) -> AccuracyReport:
    """Aggregate per-assertion outcomes into an :class:`AccuracyReport` (SPEC-Fuller-E15 §3.4)."""
    return AccuracyReport(outcomes=tuple(outcomes))


def is_executable(assertion: Assertion) -> bool:
    """Whether an assertion's ``query`` is a runnable semantic query (has ``metrics``).

    The E3 ingestion builder also emits *candidate* assertions in a raw
    ``{native, references}`` shape awaiting human completion; those are not yet
    executable by the harness and are skipped rather than treated as failures.
    """
    metrics = assertion.query.get("metrics")
    return isinstance(metrics, list) and len(metrics) > 0


def assertion_metrics(assertion: Assertion) -> list[str]:
    """The metric names an assertion's semantic query requests (``[]`` if none)."""
    metrics = assertion.query.get("metrics")
    return [str(m) for m in metrics] if isinstance(metrics, list) else []


def assertion_to_query(assertion: Assertion) -> SemanticQuery:
    """Project an assertion's ``query`` dict onto a :class:`SemanticQuery`.

    Raises:
        canonic.exc.ValidationFailed: if the query is not in executable semantic-query
            form (no ``metrics``) or fails :class:`SemanticQuery` validation.
    """
    from pydantic import ValidationError

    from canonic.compiler.query import SemanticQuery
    from canonic.exc import ValidationFailed

    if not is_executable(assertion):
        raise ValidationFailed(
            f"assertion {assertion.id!r}: query is not an executable semantic query "
            "(missing 'metrics')"
        )
    try:
        return SemanticQuery.model_validate(assertion.query)
    except ValidationError as exc:
        raise ValidationFailed(f"assertion {assertion.id!r}: invalid query — {exc}") from exc


def _as_number(value: Any) -> float | None:
    """Coerce numeric scalars (int/float/Decimal) to float; ``None`` for anything else.

    ``bool`` is an ``int`` subclass but is treated as non-numeric so ``True``/``1`` do
    not compare equal by tolerance.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float, Decimal)):
        return float(value)
    return None


def _within_tolerance(actual: Any, expected: Any, tolerance: float | None) -> bool:
    """Compare one value to its expectation, applying relative ``tolerance`` if numeric."""
    a = _as_number(actual)
    e = _as_number(expected)
    if a is None or e is None:
        return bool(actual == expected)
    if tolerance is None:
        return a == e
    if e == 0:
        return abs(a) <= tolerance  # relative tolerance undefined at 0 — fall back to absolute
    return abs(a - e) <= tolerance * abs(e)


def _fmt(values: dict[str, Any]) -> str:
    """Render an expected/actual value map like ``{revenue: 4218334.1}`` for diffs."""
    return "{" + ", ".join(f"{k}: {v}" for k, v in values.items()) + "}"


def match_result(
    assertion: Assertion,
    result: ResultSet,
    resolved: dict[str, str] | None = None,
) -> AssertionOutcome:
    """Compare an executed ``result`` to an assertion's expectation (SPEC-Fuller-E15 §3.2).

    Checks, in order: the row count (when ``expect.rows`` is set), then each expected
    column value against the first result row (within ``tolerance`` when numeric). The
    first divergence produces a failing outcome with a diff; otherwise the outcome passes.

    ``expect.values`` is keyed on the query's *names* (metric/dimension), but the compiler
    emits the underlying measure name as the SQL column. ``resolved`` (the compiler's
    ``{metric: "source.measure"}`` map) bridges that gap so an assertion stays written in
    the user's vocabulary; a key is tried as a direct column first, then via ``resolved``.
    """
    expect: AssertionExpect = assertion.expect

    if expect.rows is not None and len(result.rows) != expect.rows:
        return AssertionOutcome(
            assertion_id=assertion.id,
            passed=False,
            detail=f"{assertion.id}: expected {expect.rows} row(s), got {len(result.rows)}",
        )

    if expect.values:
        col_index = {c.name: i for i, c in enumerate(result.columns)}
        resolved = resolved or {}
        actual_values: dict[str, Any] = {}
        for col in expect.values:
            idx = col_index.get(col)
            if idx is None and col in resolved:
                idx = col_index.get(resolved[col].split(".")[-1])
            if idx is None:
                return AssertionOutcome(
                    assertion_id=assertion.id,
                    passed=False,
                    detail=(
                        f"{assertion.id}: expected column {col!r} is not in the result "
                        f"(columns: {sorted(col_index)})"
                    ),
                )
            if not result.rows:
                return AssertionOutcome(
                    assertion_id=assertion.id,
                    passed=False,
                    detail=f"{assertion.id}: expected {_fmt(dict(expect.values))}, got no rows",
                )
            actual_values[col] = result.rows[0][idx]

        for col, expected in expect.values.items():
            if not _within_tolerance(actual_values[col], expected, expect.tolerance):
                return AssertionOutcome(
                    assertion_id=assertion.id,
                    passed=False,
                    detail=(
                        f"{assertion.id}: expected {_fmt(dict(expect.values))}, "
                        f"got {_fmt(actual_values)}"
                    ),
                )

    return AssertionOutcome(assertion_id=assertion.id, passed=True)
