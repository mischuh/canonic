"""Ratios over distinct_count components (AMENDMENT-ratio-recompute-components).

A ratio used to accept only ``single`` components, so ``refunded_buyers / buyers`` failed
with "nested composite metrics are not yet supported" even though nothing is nested. The
property that matters is not the SQL shape but the number: a distinct count is not
additive, so planning one as a plain sum would compile fine and report a wrong value. The
execution tests below therefore compare every ratio against the two standalone queries
divided by hand, and pin a few values by hand as well.
"""

from __future__ import annotations

from typing import Any

import duckdb
import pytest

from canonic.compiler import SemanticQuery, compile
from canonic.contracts.models import BindingKind, CanonicalRef, MetricBinding
from canonic.contracts.resolver import ContractResolver
from canonic.exc import UnsupportedMeasure
from canonic.semantic.models import Column, Dimension, Join, Measure, Relationship, SemanticSource

_DIALECTS = {"warehouse_duckdb": "duckdb"}


@pytest.fixture
def sources() -> list[SemanticSource]:
    orders = SemanticSource(
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
        measures=[Measure(name="revenue", expr="sum(amount)", additivity="additive")],
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
    customers = SemanticSource(
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
    order_items = SemanticSource(
        name="order_items",
        connection="warehouse_duckdb",
        table="fct_order_items",
        grain=["item_id"],
        columns=[
            Column(name="item_id", type="string", nullable=False),
            Column(name="order_id", type="string", nullable=False),
            Column(name="sku", type="string", nullable=False),
        ],
        dimensions=[Dimension(name="sku", column="sku")],
    )
    return [orders, customers, order_items]


def _buyers(name: str, population_filter: str | None = None) -> MetricBinding:
    return MetricBinding(
        metric=name,
        canonical=CanonicalRef(
            kind=BindingKind.DISTINCT_COUNT,
            source="orders",
            distinct_on="customer_id",
            population_filter=population_filter,
        ),
    )


def _ratio(name: str, numerator: str, denominator: str, **extra: Any) -> MetricBinding:
    return MetricBinding(
        metric=name,
        canonical=CanonicalRef(
            kind=BindingKind.RATIO, numerator=numerator, denominator=denominator, **extra
        ),
    )


@pytest.fixture
def resolver() -> ContractResolver:
    return ContractResolver(
        bindings=[
            _buyers("buyers"),
            _buyers("refunded_buyers", "status = 'refunded'"),
            _ratio("refund_rate", "refunded_buyers", "buyers"),
            _ratio(
                "big_refund_rate", "refunded_buyers", "buyers", population_filter="amount >= 25"
            ),
            MetricBinding(
                metric="median_amount",
                canonical=CanonicalRef(
                    kind=BindingKind.PERCENTILE, source="orders", column="amount", quantile=0.5
                ),
            ),
            _ratio("amount_per_buyer", "median_amount", "buyers"),
            _ratio("refund_rate_per_buyer", "refund_rate", "buyers"),
        ],
        guardrails=[],
    )


@pytest.fixture
def con() -> duckdb.DuckDBPyConnection:
    """Five orders from four customers, one of them refunded.

    north = c1 (orders 1 paid 100, 2 refunded 50), c3 (order 4 paid 25)
    south = c2 (order 3 paid 200)
    west  = c4 (order 5 paid 10)

    Five orders but four buyers, so a distinct count that was planned as a plain row count
    reports 5 where the right answer is 4.
    """
    connection = duckdb.connect(":memory:")
    connection.execute(
        "CREATE TABLE fct_orders "
        "(order_id VARCHAR, customer_id VARCHAR, amount DECIMAL(10,2), status VARCHAR)"
    )
    connection.execute("CREATE TABLE dim_customers (customer_id VARCHAR, region VARCHAR)")
    connection.execute(
        "CREATE TABLE fct_order_items (item_id VARCHAR, order_id VARCHAR, sku VARCHAR)"
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
        "INSERT INTO fct_order_items VALUES ('a', '1', 'x'), ('b', '1', 'y'), ('c', '3', 'x')"
    )
    return connection


def _run(
    con: duckdb.DuckDBPyConnection,
    resolver: ContractResolver,
    sources: list[SemanticSource],
    metric: str,
    dimensions: list[str],
) -> dict[tuple[Any, ...], Any]:
    """Compile and execute one metric, indexed by its dimension tuple."""
    result = compile(
        SemanticQuery(metrics=[metric], dimensions=dimensions),
        resolver,
        sources,
        connection_dialects=_DIALECTS,
    )
    n = len(dimensions)
    return {tuple(row[:n]): row[n] for row in con.execute(result.sql).fetchall()}


@pytest.mark.parametrize("dimensions", [[], ["region"], ["status"], ["sku"]])
def test_ratio_equals_standalone_numerator_over_denominator(
    dimensions: list[str],
    resolver: ContractResolver,
    sources: list[SemanticSource],
    con: duckdb.DuckDBPyConnection,
) -> None:
    """At every grain the ratio is the two standalone counts divided, NULL where undefined.

    ``sku`` reaches the data through a one_to_many join, so this also pins that a distinct
    count under row fanout is not inflated. ``status`` is the case where the numerator only
    exists for one group, which must read as NULL and not as 0.
    """
    ratio = _run(con, resolver, sources, "refund_rate", dimensions)
    num = _run(con, resolver, sources, "refunded_buyers", dimensions)
    den = _run(con, resolver, sources, "buyers", dimensions)

    assert set(ratio) == set(num) | set(den), "compose dropped or invented a group"
    for group, value in ratio.items():
        n, d = num.get(group), den.get(group)
        if n is None or not d:
            assert value is None, f"{group!r}: expected NULL, got {value!r}"
        else:
            assert value == pytest.approx(n / d), f"{group!r}: {value!r} != {n}/{d}"


def test_ratio_hand_computed_values(
    resolver: ContractResolver, sources: list[SemanticSource], con: duckdb.DuckDBPyConnection
) -> None:
    """Pinned by hand, so a bug that breaks the ratio and its standalone parts alike still fails."""
    assert _run(con, resolver, sources, "refund_rate", []) == {(): pytest.approx(0.25)}

    by_region = _run(con, resolver, sources, "refund_rate", ["region"])
    assert by_region[("north",)] == pytest.approx(0.5)
    assert by_region[("south",)] is None, "no refunded buyer in south is absence, not a zero"
    assert by_region[("west",)] is None


def test_composite_population_filter_reaches_both_components(
    resolver: ContractResolver, sources: list[SemanticSource], con: duckdb.DuckDBPyConnection
) -> None:
    """``amount >= 25`` narrows buyers to c1, c2, c3 and refunded buyers to c1: 1 / 3, not 1 / 4.

    The numerator also keeps its own ``status = 'refunded'``, so the leaf has to honor the
    ratio's population and its own, not just one of the two (§4.5).
    """
    assert _run(con, resolver, sources, "big_refund_rate", []) == {(): pytest.approx(1 / 3)}


def test_component_requested_alongside_its_ratio_is_computed_once(
    resolver: ContractResolver, sources: list[SemanticSource]
) -> None:
    """``buyers`` is the ratio's denominator, so asking for both shares one leaf."""
    result = compile(
        SemanticQuery(metrics=["refund_rate", "buyers"]),
        resolver,
        sources,
        connection_dialects=_DIALECTS,
    )
    assert result.sql.upper().count("COUNT(DISTINCT") == 2, "refunded_buyers and buyers, once each"


def test_percentile_component_is_rejected_by_name_and_kind(
    resolver: ContractResolver, sources: list[SemanticSource]
) -> None:
    with pytest.raises(UnsupportedMeasure, match=r"'median_amount'.*'percentile'") as exc:
        compile(SemanticQuery(metrics=["amount_per_buyer"]), resolver, sources)
    assert "nested" not in str(exc.value), "a percentile is a leaf, nothing is nested"


def test_ratio_of_a_ratio_is_still_rejected_as_nested(
    resolver: ContractResolver, sources: list[SemanticSource]
) -> None:
    with pytest.raises(UnsupportedMeasure, match=r"nested composite.*'refund_rate'.*'ratio'"):
        compile(SemanticQuery(metrics=["refund_rate_per_buyer"]), resolver, sources)
