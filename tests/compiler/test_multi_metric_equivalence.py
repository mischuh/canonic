"""Equivalence oracle for multi-metric compose (AMENDMENT-multi-metric-compose).

The amendment generalises the composite compose step from *per metric* to *per query*:
every metric, and every leaf inside a composite, becomes its own CTE aggregating to the
requested dimensions, assembled by one outer SELECT over a shared grain spine. That is a
large refactor of `canonic/compiler/`, and the property it must never break is the only
one that actually matters to a caller:

    a metric's value in a multi-metric result is the value that metric has on its own.

This module states exactly that, by *executing* both forms against real data and
comparing per dimension tuple, rather than asserting on SQL shape. Shape assertions live
in `test_multi_metric_compose.py`; they tell you the compiler emitted the plan you meant,
but only execution tells you the plan was right. Written before the refactor starts so
every step of it is falsifiable (S11 AC3, and the guard rail for S12 AC3 and S13 AC2).

A dimension value present for one metric and absent for another must read as NULL, not 0
and not a dropped row (amendment §4.2) — `west` below has an order but no order items and
exists to pin that.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import duckdb
import pytest

from canonic.compiler import SemanticQuery, compile
from canonic.contracts.models import (
    AppliesTo,
    CanonicalRef,
    Guardrail,
    GuardrailKind,
    MetricBinding,
    Severity,
)
from canonic.contracts.resolver import ContractResolver
from canonic.semantic.models import Column, Dimension, Join, Measure, Relationship, SemanticSource

if TYPE_CHECKING:
    from canonic.compiler.result import CompileResult

_DIALECTS = {"warehouse_duckdb": "duckdb"}


@pytest.fixture
def orders() -> SemanticSource:
    """Order-grain fact: two additive measures, many_to_one to customers, one_to_many to items."""
    return SemanticSource(
        name="orders",
        connection="warehouse_duckdb",
        table="fct_orders",
        grain=["order_id"],
        columns=[
            Column(name="order_id", type="string", nullable=False),
            Column(name="customer_id", type="string", nullable=False),
            Column(name="amount", type="decimal", nullable=True),
            Column(name="status", type="string", nullable=False),
        ],
        measures=[
            Measure(name="revenue", expr="sum(amount)", additivity="additive"),
            Measure(name="order_count", expr="count(*)", additivity="additive"),
        ],
        dimensions=[Dimension(name="status", column="status")],
        joins=[
            Join(
                to="customers",
                on="orders.customer_id = customers.customer_id",
                relationship=Relationship.MANY_TO_ONE,
            ),
            Join(
                to="order_items",
                on="orders.order_id = order_items.order_id",
                relationship=Relationship.ONE_TO_MANY,
            ),
        ],
    )


@pytest.fixture
def customers() -> SemanticSource:
    return SemanticSource(
        name="customers",
        connection="warehouse_duckdb",
        table="dim_customers",
        grain=["customer_id"],
        columns=[
            Column(name="customer_id", type="string", nullable=False),
            Column(name="region", type="string", nullable=False),
        ],
        dimensions=[Dimension(name="region", column="region")],
    )


@pytest.fixture
def order_items() -> SemanticSource:
    """Item-grain fact. Declares its own join back to orders so `region` is reachable
    from a leaf rooted here — joins are one-directional (SPEC §10), so without it a
    leaf rooted at order_items genuinely cannot project a customers dimension."""
    return SemanticSource(
        name="order_items",
        connection="warehouse_duckdb",
        table="fct_order_items",
        grain=["item_id"],
        columns=[
            Column(name="item_id", type="string", nullable=False),
            Column(name="order_id", type="string", nullable=False),
            Column(name="quantity", type="decimal", nullable=True),
            Column(name="sku", type="string", nullable=False),
        ],
        measures=[Measure(name="units_sold", expr="sum(quantity)", additivity="additive")],
        dimensions=[Dimension(name="sku", column="sku")],
        joins=[
            Join(
                to="orders",
                on="order_items.order_id = orders.order_id",
                relationship=Relationship.MANY_TO_ONE,
            )
        ],
    )


@pytest.fixture
def sources(
    orders: SemanticSource, customers: SemanticSource, order_items: SemanticSource
) -> list[SemanticSource]:
    return [orders, customers, order_items]


@pytest.fixture
def resolver() -> ContractResolver:
    """Four metrics chosen to exercise every leaf relationship the compose step can see.

    ``revenue`` and ``paid_revenue`` share a source *and* a measure and differ only by
    ``population_filter`` — the pair a too-loose leaf-dedup key would wrongly collapse
    (amendment §3.2). ``order_count`` carries a mandatory_filter guardrail, so it differs
    from ``revenue`` in effective filters rather than in population.
    """
    return ContractResolver(
        bindings=[
            MetricBinding(
                metric="revenue",
                canonical=CanonicalRef(source="orders", measure="revenue"),
            ),
            MetricBinding(
                metric="paid_revenue",
                canonical=CanonicalRef(
                    source="orders", measure="revenue", population_filter="status = 'paid'"
                ),
            ),
            MetricBinding(
                metric="order_count",
                canonical=CanonicalRef(source="orders", measure="order_count"),
            ),
            MetricBinding(
                metric="units_sold",
                canonical=CanonicalRef(source="order_items", measure="units_sold"),
            ),
        ],
        guardrails=[
            Guardrail(
                id="order-count-excludes-refunds",
                applies_to=AppliesTo(source="orders", measure="order_count"),
                kind=GuardrailKind.MANDATORY_FILTER,
                filter="status != 'refunded'",
                severity=Severity.ERROR,
                rationale="A refunded order is a reversal, not an order.",
            )
        ],
    )


@pytest.fixture
def con() -> duckdb.DuckDBPyConnection:
    """Seed data chosen so every metric has a distinct, hand-checkable value per region.

    north = orders 1 (paid 100), 2 (refunded 50), 4 (paid 25)
    south = order 3 (paid 200)
    west  = order 5 (paid 10), which has NO order items — the absent-row case (§4.2)
    """
    connection = duckdb.connect(":memory:")
    connection.execute(
        "CREATE TABLE fct_orders "
        "(order_id VARCHAR, customer_id VARCHAR, amount DECIMAL(10,2), status VARCHAR)"
    )
    connection.execute("CREATE TABLE dim_customers (customer_id VARCHAR, region VARCHAR)")
    connection.execute(
        "CREATE TABLE fct_order_items "
        "(item_id VARCHAR, order_id VARCHAR, quantity DECIMAL(10,2), sku VARCHAR)"
    )
    connection.execute(
        "INSERT INTO fct_orders VALUES "
        "('1', 'c1', 100, 'paid'), ('2', 'c1', 50, 'refunded'), ('3', 'c2', 200, 'paid'), "
        "('4', 'c3', 25, 'paid'), ('5', 'c4', 10, 'paid')"
    )
    connection.execute(
        "INSERT INTO dim_customers VALUES "
        "('c1', 'north'), ('c2', 'south'), ('c3', 'north'), ('c4', 'west')"
    )
    connection.execute(
        "INSERT INTO fct_order_items VALUES "
        "('a', '1', 2, 'x'), ('b', '1', 3, 'y'), ('c', '3', 4, 'x')"
    )
    return connection


def _compile(
    metrics: list[str],
    dimensions: list[str],
    resolver: ContractResolver,
    sources: list[SemanticSource],
) -> CompileResult:
    return compile(
        SemanticQuery(metrics=metrics, dimensions=dimensions),
        resolver,
        sources,
        connection_dialects=_DIALECTS,
    )


def _by_dims(
    con: duckdb.DuckDBPyConnection, sql: str, n_dims: int, metric_index: int
) -> dict[tuple[Any, ...], Any]:
    """Execute and index one metric column by its dimension tuple.

    Columns are read positionally, not by name: two metrics bound to the same measure
    (``revenue`` and ``paid_revenue``) currently emit two columns of the same name, so
    name lookup would silently read the wrong one.
    """
    rows = con.execute(sql).fetchall()
    return {tuple(row[:n_dims]): row[n_dims + metric_index] for row in rows}


# Every combination that compiles on `main` today, so the oracle can run before, during,
# and after the refactor. Each names the leaf relationship it is there to pin.
#
# `same-source-own-dim` was an xfail until conditional aggregation was retired: grouping
# {revenue, order_count} by status used to emit
#   COUNT(CASE WHEN status <> 'refunded' THEN 1 END) ... GROUP BY status
# so the `refunded` group survived (revenue needs it) and order_count claimed a measured
# 0 there, while `--metrics order_count --dimensions status` filters that group away
# entirely. Now each metric owns its leaf, the grain spine unions both status sets, and
# the absent group reads NULL — absence of rows is not a measured zero (§4.2).
_COMBINATIONS = [
    pytest.param(["revenue", "order_count"], [], id="same-source-scalar"),
    pytest.param(["revenue", "order_count"], ["region"], id="same-source-joined-dim"),
    pytest.param(["revenue", "order_count"], ["status"], id="same-source-own-dim"),
    pytest.param(["revenue", "paid_revenue"], [], id="same-measure-differing-population"),
    pytest.param(
        ["revenue", "paid_revenue"], ["region"], id="same-measure-differing-population-dim"
    ),
    pytest.param(["revenue", "units_sold"], [], id="cross-source-fanout-scalar"),
    pytest.param(["revenue", "units_sold"], ["region"], id="cross-source-fanout-dim"),
    pytest.param(["revenue", "units_sold"], ["sku"], id="cross-source-dim-on-many-side"),
    pytest.param(
        ["revenue", "order_count", "units_sold"], ["region"], id="three-metrics-two-sources"
    ),
    pytest.param(
        ["paid_revenue", "units_sold"], ["region"], id="population-filter-plus-cross-source"
    ),
]


@pytest.mark.parametrize(("metrics", "dimensions"), _COMBINATIONS)
def test_multi_metric_values_match_single_metric_queries(
    metrics: list[str],
    dimensions: list[str],
    resolver: ContractResolver,
    sources: list[SemanticSource],
    con: duckdb.DuckDBPyConnection,
) -> None:
    """Each column of a multi-metric result equals that metric queried on its own (S11 AC3).

    A dimension tuple the single-metric query never produces is compared against None,
    which is what the multi-metric form must report for it — not 0, and not a dropped row
    (amendment §4.2, S13 AC2).
    """
    multi = _compile(metrics, dimensions, resolver, sources)
    n_dims = len(dimensions)

    for i, metric in enumerate(metrics):
        single = _compile([metric], dimensions, resolver, sources)
        expected = _by_dims(con, single.sql, n_dims, 0)
        actual = _by_dims(con, multi.sql, n_dims, i)
        for dim_tuple, value in actual.items():
            assert value == expected.get(dim_tuple), (
                f"metric {metric!r} at {dim_tuple!r}: multi-metric gave {value!r}, "
                f"standalone gave {expected.get(dim_tuple)!r}"
            )
        # Every group the standalone query found must survive compose (no dropped rows).
        assert set(expected) <= set(actual), (
            f"metric {metric!r}: compose dropped groups {set(expected) - set(actual)!r}"
        )


def test_absent_leaf_row_reads_as_null_not_zero(
    resolver: ContractResolver, sources: list[SemanticSource], con: duckdb.DuckDBPyConnection
) -> None:
    """`west` has an order but no order items: units_sold is NULL there, and the row stays.

    Hardcoded rather than derived, so a refactor that breaks *both* the multi-metric and
    the single-metric form in the same direction still fails (amendment §4.2, S13 AC2).
    """
    result = _compile(["revenue", "units_sold"], ["region"], resolver, sources)
    rows = {row[0]: (row[1], row[2]) for row in con.execute(result.sql).fetchall()}

    assert set(rows) == {"north", "south", "west"}
    assert rows["north"] == (175, 5)
    assert rows["south"] == (200, 4)
    assert rows["west"][0] == 10
    assert rows["west"][1] is None, "absence of rows is not a measured zero"


def test_guardrail_and_population_filter_stay_scoped_to_their_own_metric(
    resolver: ContractResolver, sources: list[SemanticSource], con: duckdb.DuckDBPyConnection
) -> None:
    """Hand-computed totals for three metrics whose effective filters all differ.

    `revenue` sees every order (385), `paid_revenue` excludes the refund via
    population_filter (335), `order_count` excludes it via a mandatory_filter guardrail
    (4 of 5). If any one of those restrictions leaks onto a sibling metric, at least two
    of these three numbers move.
    """
    result = _compile(["revenue", "paid_revenue", "order_count"], [], resolver, sources)
    row = con.execute(result.sql).fetchone()

    assert row is not None
    assert row == (385, 335, 4)
