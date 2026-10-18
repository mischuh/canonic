"""A ratio flags the rows whose zero denominator it replaced, in a column the service drops."""

from __future__ import annotations

from typing import Any

import duckdb
import pytest

from canonic.compiler import SemanticQuery, compile
from canonic.compiler.result import PROBE_ZERO_DENOMINATOR, CompileResult
from canonic.connectors.base import ResultColumn, ResultSet
from canonic.contracts.models import BindingKind, CanonicalRef, MetricBinding, OnZeroDenominator
from canonic.contracts.resolver import ContractResolver
from canonic.core.probes import strip_probes
from canonic.semantic.models import Column, Dimension, Measure, SemanticSource

#: category "b" books shipping but no amount, so its denominator is zero. "c" has no shipping.
_ORDERS = [(1, "a", 10.0, 1.0), (2, "b", 0.0, 2.0), (3, "b", 0.0, 3.0), (4, "c", 5.0, 0.0)]

_SOURCE = SemanticSource(
    name="orders",
    connection="wh",
    table="orders",
    grain=["order_id"],
    columns=[
        Column(name="order_id", type="int", nullable=False),
        Column(name="category", type="string", nullable=False),
        Column(name="amount", type="decimal", nullable=False),
        Column(name="shipping", type="decimal", nullable=False),
    ],
    measures=[
        Measure(name="total_amount", expr="sum(amount)", additivity="additive"),
        Measure(name="total_shipping", expr="sum(shipping)", additivity="additive"),
    ],
    dimensions=[Dimension(name="category", column="category")],
)


def _resolver(on_zero: OnZeroDenominator) -> ContractResolver:
    return ContractResolver(
        bindings=[
            MetricBinding(
                metric="amount", canonical=CanonicalRef(source="orders", measure="total_amount")
            ),
            MetricBinding(
                metric="shipping",
                canonical=CanonicalRef(source="orders", measure="total_shipping"),
            ),
            MetricBinding(
                metric="shipping_share",
                aliases=["share"],
                canonical=CanonicalRef(
                    kind=BindingKind.RATIO,
                    numerator="shipping",
                    denominator="amount",
                    on_zero_denominator=on_zero,
                ),
            ),
        ],
        guardrails=[],
    )


def _compile(on_zero: OnZeroDenominator, *metrics: str, dimensions: list[str] | None = None):
    return compile(
        SemanticQuery(metrics=list(metrics), dimensions=dimensions or []),
        _resolver(on_zero),
        [_SOURCE],
        connection_dialects={"wh": "duckdb"},
    )


def _execute(compiled: CompileResult) -> ResultSet:
    con = duckdb.connect()
    con.execute("CREATE TABLE orders (order_id INT, category TEXT, amount DOUBLE, shipping DOUBLE)")
    con.executemany("INSERT INTO orders VALUES (?, ?, ?, ?)", _ORDERS)
    cursor = con.execute(compiled.sql)
    return ResultSet(
        columns=[ResultColumn(name=d[0], type="any") for d in cursor.description],
        rows=[list(row) for row in cursor.fetchall()],
    )


@pytest.mark.parametrize("on_zero", [OnZeroDenominator.NULL, OnZeroDenominator.ZERO])
def test_substituting_policies_add_a_probe_column(on_zero: OnZeroDenominator) -> None:
    compiled = _compile(on_zero, "shipping_share", dimensions=["category"])

    (probe,) = compiled.probe_columns
    assert (probe.kind, probe.subject) == (PROBE_ZERO_DENOMINATOR, "shipping_share")
    assert probe.column == "_canonic_zero_denominator_shipping_share"
    assert probe.column in compiled.sql


def test_error_policy_adds_no_probe() -> None:
    compiled = _compile(OnZeroDenominator.ERROR, "shipping_share", dimensions=["category"])

    assert compiled.probe_columns == ()
    assert "_canonic_zero_denominator" not in compiled.sql


def test_plain_metrics_add_no_probe() -> None:
    compiled = _compile(OnZeroDenominator.NULL, "amount", "shipping", dimensions=["category"])

    assert compiled.probe_columns == ()


def test_probe_subject_is_the_name_the_caller_asked_for() -> None:
    compiled = _compile(OnZeroDenominator.NULL, "share", dimensions=["category"])

    (probe,) = compiled.probe_columns
    assert probe.subject == "share"
    assert probe.column == "_canonic_zero_denominator_shipping_share"


@pytest.mark.parametrize(
    ("on_zero", "substituted"), [(OnZeroDenominator.NULL, None), (OnZeroDenominator.ZERO, 0)]
)
def test_the_flag_counts_exactly_the_rows_with_a_zero_denominator(
    on_zero: OnZeroDenominator, substituted: Any
) -> None:
    compiled = _compile(on_zero, "shipping_share", dimensions=["category"])

    result, counts = strip_probes(compiled, _execute(compiled))

    assert counts == {(PROBE_ZERO_DENOMINATOR, "shipping_share"): 1}
    assert [c.name for c in result.columns] == ["category", "shipping_share"]
    by_category = {row[0]: row[1] for row in result.rows}
    assert by_category["b"] == substituted
    assert by_category["a"] == pytest.approx(0.1)


def test_a_real_zero_is_not_counted() -> None:
    """Category "c" has a zero numerator over a non-zero denominator: a real 0, no flag."""
    compiled = _compile(OnZeroDenominator.ZERO, "shipping_share", dimensions=["category"])

    result, counts = strip_probes(compiled, _execute(compiled))

    assert {row[0]: row[1] for row in result.rows}["c"] == 0
    assert counts[(PROBE_ZERO_DENOMINATOR, "shipping_share")] == 1


def test_scalar_ratio_with_a_zero_denominator_counts_one_row() -> None:
    compiled = _compile(OnZeroDenominator.NULL, "shipping_share")
    zero_only = ResultSet(
        columns=[
            ResultColumn(name="shipping_share", type="float"),
            ResultColumn(name="_canonic_zero_denominator_shipping_share", type="int"),
        ],
        rows=[[None, 1]],
    )

    _, counts = strip_probes(compiled, zero_only)

    assert counts == {(PROBE_ZERO_DENOMINATOR, "shipping_share"): 1}
