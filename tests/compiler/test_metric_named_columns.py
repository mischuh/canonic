"""Every result column is named after the metric the caller asked for, never the measure.

A caller only knows metric names, and several metrics can bind to one measure. Each test
executes the compiled SQL on DuckDB and reads the result by column name, so a column that
is misnamed, missing, or carrying another metric's value fails on the number.
"""

from __future__ import annotations

from typing import Any

import duckdb
import pytest

from canonic.compiler import SemanticQuery, compile
from canonic.contracts.models import BindingKind, CanonicalRef, CollapseAgg, MetricBinding
from canonic.contracts.resolver import ContractResolver
from canonic.semantic.models import Column, Dimension, Measure, SemanticSource

_ORDERS = [
    (1, "2025-01-01", "a", 10.0, 1.0),
    (2, "2025-01-01", "b", 20.0, 2.0),
    (3, "2025-01-02", "a", 30.0, 3.0),
]
_SNAPSHOTS = [
    ("w1", "2025-01-01", 5.0),
    ("w1", "2025-01-02", 7.0),
    ("w2", "2025-01-01", 3.0),
    ("w2", "2025-01-02", 4.0),
]


def _sources() -> list[SemanticSource]:
    orders = SemanticSource(
        name="orders",
        connection="wh",
        table="orders",
        grain=["order_id"],
        columns=[
            Column(name="order_id", type="int", nullable=False),
            Column(name="order_date", type="date", nullable=False),
            Column(name="category", type="string", nullable=False),
            Column(name="amount", type="decimal", nullable=False),
            Column(name="shipping", type="decimal", nullable=False),
        ],
        # The measure "revenue" sums shipping on purpose: the metric "revenue" binds to
        # total_revenue, so a column named after the measure would collide with it.
        measures=[
            Measure(name="total_revenue", expr="sum(amount)", additivity="additive"),
            Measure(name="revenue", expr="sum(shipping)", additivity="additive"),
        ],
        dimensions=[
            Dimension(name="order_day", column="order_date", granularity="day"),
            Dimension(name="category", column="category"),
        ],
    )
    snapshots = SemanticSource(
        name="snapshots",
        connection="wh",
        table="snapshots",
        grain=["warehouse_id", "snapshot_date"],
        columns=[
            Column(name="warehouse_id", type="string", nullable=False),
            Column(name="snapshot_date", type="date", nullable=False),
            Column(name="level", type="decimal", nullable=False),
        ],
        measures=[Measure(name="stock_level", expr="sum(level)", additivity="additive")],
        dimensions=[
            Dimension(name="warehouse_id", column="warehouse_id"),
            Dimension(name="snapshot_date", column="snapshot_date", granularity="day"),
        ],
    )
    return [orders, snapshots]


def _semi(metric: str) -> MetricBinding:
    return MetricBinding(
        metric=metric,
        canonical=CanonicalRef(
            kind=BindingKind.SEMI_ADDITIVE,
            source="snapshots",
            measure="stock_level",
            collapse_dimension="snapshot_date",
            collapse_agg=CollapseAgg.LAST,
        ),
    )


def _resolver() -> ContractResolver:
    return ContractResolver(
        bindings=[
            MetricBinding(
                metric="revenue", canonical=CanonicalRef(source="orders", measure="total_revenue")
            ),
            MetricBinding(
                metric="shipping", canonical=CanonicalRef(source="orders", measure="revenue")
            ),
            MetricBinding(
                metric="revenue_a",
                canonical=CanonicalRef(
                    source="orders", measure="total_revenue", population_filter="category = 'a'"
                ),
            ),
            MetricBinding(
                metric="shipping_share",
                canonical=CanonicalRef(kind="ratio", numerator="shipping", denominator="revenue"),
            ),
            MetricBinding(
                metric="running_revenue",
                canonical=CanonicalRef(
                    kind="cumulative",
                    source="orders",
                    measure="total_revenue",
                    order_by=["order_day"],
                ),
            ),
            _semi("ending_stock"),
            _semi("closing_stock"),
        ],
        guardrails=[],
    )


@pytest.fixture
def run() -> Any:
    con = duckdb.connect()
    con.execute(
        "CREATE TABLE orders (order_id INT, order_date DATE, category TEXT, "
        "amount DOUBLE, shipping DOUBLE)"
    )
    con.executemany("INSERT INTO orders VALUES (?, ?, ?, ?, ?)", _ORDERS)
    con.execute("CREATE TABLE snapshots (warehouse_id TEXT, snapshot_date DATE, level DOUBLE)")
    con.executemany("INSERT INTO snapshots VALUES (?, ?, ?)", _SNAPSHOTS)

    def _run(metrics: list[str], dimensions: list[str] | None = None) -> list[dict[str, Any]]:
        result = compile(
            SemanticQuery(metrics=metrics, dimensions=dimensions or []),
            _resolver(),
            _sources(),
            connection_dialects={"wh": "duckdb"},
        )
        cursor = con.execute(result.sql)
        names = [d[0] for d in cursor.description]
        assert len(names) == len(set(names)), f"duplicate output columns: {names}"
        rows = [dict(zip(names, row, strict=True)) for row in cursor.fetchall()]
        return sorted(rows, key=lambda r: tuple(str(v) for v in r.values()))

    return _run


def test_single_metric_is_named_after_the_metric(run: Any) -> None:
    assert run(["revenue"]) == [{"revenue": 60.0}]


def test_metrics_sharing_a_measure_get_their_own_columns(run: Any) -> None:
    rows = run(["revenue", "revenue_a"], ["category"])
    assert rows == [
        {"category": "a", "revenue": 40.0, "revenue_a": 40.0},
        {"category": "b", "revenue": 20.0, "revenue_a": None},
    ]


def test_metric_and_unrelated_measure_of_the_same_name_do_not_collide(run: Any) -> None:
    # The ratio's denominator is the metric "revenue" (sum of amount); its numerator binds
    # to the measure called "revenue" (sum of shipping). Fused into one leaf, both were once
    # projected as "revenue", and dedup kept only one of them.
    rows = run(["revenue", "shipping", "shipping_share"])
    assert rows == [{"revenue": 60.0, "shipping": 6.0, "shipping_share": pytest.approx(0.1)}]


def test_cumulative_metric_is_named_after_the_metric(run: Any) -> None:
    rows = run(["revenue", "running_revenue"], ["order_day"])
    assert [(r["revenue"], r["running_revenue"]) for r in rows] == [(30.0, 30.0), (30.0, 60.0)]


@pytest.mark.parametrize(
    ("dimensions", "expected"),
    [
        (["warehouse_id"], [7.0, 4.0]),
        (["snapshot_date"], [8.0, 11.0]),
    ],
    ids=["collapsed", "grouped_by_collapse_dimension"],
)
def test_semi_additive_is_named_after_the_metric_in_both_branches(
    run: Any, dimensions: list[str], expected: list[float]
) -> None:
    rows = run(["ending_stock"], dimensions)
    assert [r["ending_stock"] for r in rows] == expected


@pytest.mark.parametrize("dimensions", [["warehouse_id"], ["snapshot_date"]])
def test_two_semi_additive_metrics_with_one_plan_both_carry_values(
    run: Any, dimensions: list[str]
) -> None:
    rows = run(["ending_stock", "closing_stock"], dimensions)
    assert rows
    assert all(r["ending_stock"] == r["closing_stock"] is not None for r in rows)
