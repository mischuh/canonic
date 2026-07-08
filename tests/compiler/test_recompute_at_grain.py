"""Compiler tests for recompute_at_grain strategy — distinct_count & percentile (GH-120, S4).

Acceptance criteria:
  AC1 (S4): active_customers by week → count(distinct customer_id) recomputed at grain;
            never summed from partial counts; SQL contains COUNT(DISTINCT …) + GROUP BY.
  AC2 (S4): median_order_value by region → quantile computed per region from base rows;
            SQL contains PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY …) + GROUP BY.
  AC3 (S2b-AC4): active_customers with population_filter → filter applied before
                 COUNT(DISTINCT …) at every grain.
  Fanout split: distinct_count tolerates fanning joins (DISTINCT dedups);
                percentile rejects them with FANOUT_UNSAFE.
"""

from __future__ import annotations

import pytest
import sqlglot

from canonic import exc
from canonic.compiler import SemanticQuery, compile
from canonic.contracts.models import BindingKind, CanonicalRef, MetricBinding
from canonic.contracts.resolver import ContractResolver
from canonic.semantic.models import Column, Dimension, Join, Measure, Relationship, SemanticSource

# ---------------------------------------------------------------------------
# Fixtures — in-memory orders project for recompute_at_grain tests
# ---------------------------------------------------------------------------


@pytest.fixture
def orders_rg() -> SemanticSource:
    """Fact at order grain: customer_id and amount columns, week + region dimensions."""
    return SemanticSource(
        name="orders",
        connection="warehouse_pg",
        table="analytics.fct_orders",
        grain=["order_id"],
        columns=[
            Column(name="order_id", type="string", nullable=False),
            Column(name="customer_id", type="string", nullable=False),
            Column(name="amount", type="decimal", nullable=False),
            Column(name="created_at", type="timestamp", nullable=False),
        ],
        measures=[
            Measure(name="order_count", expr="count(order_id)", additivity="additive"),
        ],
        dimensions=[
            Dimension(name="week", column="created_at", granularity="week"),
        ],
        joins=[
            Join(
                to="customers_rg",
                on="orders.customer_id = customers_rg.customer_id",
                relationship=Relationship.MANY_TO_ONE,
            ),
            Join(
                to="order_items_rg",
                on="orders.order_id = order_items_rg.order_id",
                relationship=Relationship.ONE_TO_MANY,
            ),
        ],
    )


@pytest.fixture
def customers_rg() -> SemanticSource:
    """Dimension table joined many_to_one from orders — no fanout."""
    return SemanticSource(
        name="customers_rg",
        connection="warehouse_pg",
        table="analytics.dim_customers",
        grain=["customer_id"],
        columns=[
            Column(name="customer_id", type="string", nullable=False),
            Column(name="region", type="string", nullable=False),
        ],
        dimensions=[Dimension(name="region", column="region")],
    )


@pytest.fixture
def order_items_rg() -> SemanticSource:
    """One_to_many join target — fans out the order grain."""
    return SemanticSource(
        name="order_items_rg",
        connection="warehouse_pg",
        table="analytics.fct_order_items",
        grain=["item_id"],
        columns=[
            Column(name="item_id", type="string", nullable=False),
            Column(name="order_id", type="string", nullable=False),
            Column(name="sku", type="string", nullable=False),
        ],
        dimensions=[Dimension(name="sku", column="sku")],
    )


@pytest.fixture
def active_customers_binding() -> MetricBinding:
    return MetricBinding(
        metric="active_customers",
        canonical=CanonicalRef(
            kind=BindingKind.DISTINCT_COUNT,
            source="orders",
            distinct_on="customer_id",
        ),
        aliases=["unique customers"],
    )


@pytest.fixture
def median_order_value_binding() -> MetricBinding:
    return MetricBinding(
        metric="median_order_value",
        canonical=CanonicalRef(
            kind=BindingKind.PERCENTILE,
            source="orders",
            column="amount",
            quantile=0.5,
        ),
    )


@pytest.fixture
def rg_resolver(
    active_customers_binding: MetricBinding,
    median_order_value_binding: MetricBinding,
) -> ContractResolver:
    return ContractResolver(
        bindings=[active_customers_binding, median_order_value_binding],
        guardrails=[],
    )


def _parse_ok(sql: str) -> None:
    sqlglot.parse_one(sql, dialect="postgres")


# ---------------------------------------------------------------------------
# AC1 — distinct_count recomputes at grain (never sums partials)
# ---------------------------------------------------------------------------


def test_ac1_distinct_count_by_week(
    rg_resolver: ContractResolver,
    orders_rg: SemanticSource,
) -> None:
    """active_customers by week → COUNT(DISTINCT customer_id) grouped by week bucket."""
    result = compile(
        SemanticQuery(metrics=["active_customers"], dimensions=["week"]),
        rg_resolver,
        [orders_rg],
    )
    _parse_ok(result.sql)
    sql_upper = result.sql.upper()
    assert "COUNT(DISTINCT" in sql_upper
    assert "GROUP BY" in sql_upper
    assert "DISTINCT ON" not in sql_upper  # not the dedup-join form
    assert "SUM(" not in sql_upper  # never sums partial distinct counts
    assert result.recompute_at_grain is not None
    assert result.recompute_at_grain.kind == "distinct_count"
    assert result.recompute_at_grain.distinct_on == "customer_id"
    assert result.recompute_at_grain.column is None
    assert result.recompute_at_grain.quantile is None
    assert result.resolved == {"active_customers": "recompute_at_grain(orders.customer_id)"}


def test_ac1_distinct_count_scalar(
    rg_resolver: ContractResolver,
    orders_rg: SemanticSource,
) -> None:
    """Scalar (no dims) → COUNT(DISTINCT customer_id) with no GROUP BY."""
    result = compile(
        SemanticQuery(metrics=["active_customers"]),
        rg_resolver,
        [orders_rg],
    )
    _parse_ok(result.sql)
    sql_upper = result.sql.upper()
    assert "COUNT(DISTINCT" in sql_upper
    assert "GROUP BY" not in sql_upper


def test_ac1_resolved_by_alias(
    rg_resolver: ContractResolver,
    orders_rg: SemanticSource,
) -> None:
    """distinct_count metric resolves when queried by alias."""
    result = compile(
        SemanticQuery(metrics=["unique customers"]),
        rg_resolver,
        [orders_rg],
    )
    _parse_ok(result.sql)
    assert "unique customers" in result.resolved


# ---------------------------------------------------------------------------
# AC2 — percentile recomputes at grain
# ---------------------------------------------------------------------------


def test_ac2_percentile_by_region(
    rg_resolver: ContractResolver,
    orders_rg: SemanticSource,
    customers_rg: SemanticSource,
) -> None:
    """median_order_value by region → PERCENTILE_CONT(0.5) WITHIN GROUP grouped by region."""
    result = compile(
        SemanticQuery(metrics=["median_order_value"], dimensions=["region"]),
        rg_resolver,
        [orders_rg, customers_rg],
    )
    _parse_ok(result.sql)
    sql_upper = result.sql.upper()
    assert "PERCENTILE_CONT" in sql_upper
    assert "WITHIN GROUP" in sql_upper
    assert "ORDER BY" in sql_upper
    assert "GROUP BY" in sql_upper
    assert "SUM(" not in sql_upper  # never averages partials
    assert result.recompute_at_grain is not None
    assert result.recompute_at_grain.kind == "percentile"
    assert result.recompute_at_grain.column == "amount"
    assert result.recompute_at_grain.quantile == 0.5
    assert result.recompute_at_grain.distinct_on is None


def test_ac2_percentile_scalar(
    rg_resolver: ContractResolver,
    orders_rg: SemanticSource,
) -> None:
    """Scalar percentile → PERCENTILE_CONT with no GROUP BY."""
    result = compile(
        SemanticQuery(metrics=["median_order_value"]),
        rg_resolver,
        [orders_rg],
    )
    _parse_ok(result.sql)
    assert "PERCENTILE_CONT" in result.sql.upper()
    assert "GROUP BY" not in result.sql.upper()


def test_ac2_non_median_quantile() -> None:
    """p95 (quantile=0.95) → PERCENTILE_CONT(0.95)."""
    p95_binding = MetricBinding(
        metric="p95_order_value",
        canonical=CanonicalRef(
            kind=BindingKind.PERCENTILE,
            source="orders",
            column="amount",
            quantile=0.95,
        ),
    )
    resolver = ContractResolver(bindings=[p95_binding], guardrails=[])
    orders_rg = SemanticSource(
        name="orders",
        connection="warehouse_pg",
        table="analytics.fct_orders",
        grain=["order_id"],
        columns=[
            Column(name="order_id", type="string", nullable=False),
            Column(name="amount", type="decimal", nullable=False),
        ],
        measures=[],
        dimensions=[],
    )
    result = compile(SemanticQuery(metrics=["p95_order_value"]), resolver, [orders_rg])
    _parse_ok(result.sql)
    assert "0.95" in result.sql
    assert result.recompute_at_grain is not None
    assert result.recompute_at_grain.quantile == 0.95


# ---------------------------------------------------------------------------
# AC2b — SQLite has no PERCENTILE_CONT: falls back to a CUME_DIST() window query
# ---------------------------------------------------------------------------


def test_percentile_sqlite_fallback_by_region(
    rg_resolver: ContractResolver,
    orders_rg: SemanticSource,
    customers_rg: SemanticSource,
) -> None:
    """On sqlite, median_order_value by region uses CUME_DIST() instead of PERCENTILE_CONT."""
    result = compile(
        SemanticQuery(metrics=["median_order_value"], dimensions=["region"]),
        rg_resolver,
        [orders_rg, customers_rg],
        connection_dialects={"warehouse_pg": "sqlite"},
    )
    sqlglot.parse_one(result.sql, dialect="sqlite")
    sql_upper = result.sql.upper()
    assert "PERCENTILE_CONT" not in sql_upper
    assert "CUME_DIST" in sql_upper
    assert "GROUP BY" in sql_upper
    assert result.recompute_at_grain is not None
    assert result.recompute_at_grain.kind == "percentile"
    assert result.recompute_at_grain.quantile == 0.5
    assert len(result.warnings) == 1
    assert "nearest-rank" in result.warnings[0]


def test_percentile_sqlite_fallback_scalar(
    rg_resolver: ContractResolver,
    orders_rg: SemanticSource,
) -> None:
    """Scalar (no dims) sqlite percentile still uses the CUME_DIST() fallback, no GROUP BY."""
    result = compile(
        SemanticQuery(metrics=["median_order_value"]),
        rg_resolver,
        [orders_rg],
        connection_dialects={"warehouse_pg": "sqlite"},
    )
    sqlglot.parse_one(result.sql, dialect="sqlite")
    sql_upper = result.sql.upper()
    assert "CUME_DIST" in sql_upper
    assert "GROUP BY" not in sql_upper


def test_percentile_sqlite_fallback_role_qualified_dims_distinct() -> None:
    """Regression: percentile fallback must not collide same-named dims from different roles.

    Mirrors a rentals-style query — 'pickup.city' and 'dropoff.city' both resolve to the
    same underlying 'city' dimension on a 'locations' source joined twice under different
    roles. Aliasing both to bare 'city' in the CUME_DIST() CTE collapses the SELECT/GROUP BY
    to a single column, so every row spuriously shows the same pickup and dropoff city.
    """
    locations = SemanticSource(
        name="locations",
        connection="warehouse_sqlite",
        table="analytics.locations",
        grain=["location_id"],
        columns=[
            Column(name="location_id", type="string", nullable=False),
            Column(name="city", type="string", nullable=False),
        ],
        dimensions=[Dimension(name="city", column="city")],
    )
    rentals = SemanticSource(
        name="rentals",
        connection="warehouse_sqlite",
        table="analytics.rentals",
        grain=["rental_id"],
        columns=[
            Column(name="rental_id", type="string", nullable=False),
            Column(name="pickup_location_id", type="string", nullable=False),
            Column(name="dropoff_location_id", type="string", nullable=False),
            Column(name="total_amount", type="decimal", nullable=False),
        ],
        joins=[
            Join(
                name="pickup",
                to="locations",
                on="rentals.pickup_location_id = locations.location_id",
                relationship=Relationship.MANY_TO_ONE,
            ),
            Join(
                name="dropoff",
                to="locations",
                on="rentals.dropoff_location_id = locations.location_id",
                relationship=Relationship.MANY_TO_ONE,
            ),
        ],
    )
    binding = MetricBinding(
        metric="median_rental_amount",
        canonical=CanonicalRef(
            kind=BindingKind.PERCENTILE,
            source="rentals",
            column="total_amount",
            quantile=0.5,
        ),
    )
    resolver = ContractResolver(bindings=[binding], guardrails=[])

    result = compile(
        SemanticQuery(metrics=["median_rental_amount"], dimensions=["pickup.city", "dropoff.city"]),
        resolver,
        [rentals, locations],
        connection_dialects={"warehouse_sqlite": "sqlite"},
    )
    parsed = sqlglot.parse_one(result.sql, dialect="sqlite")
    outer_select = parsed if isinstance(parsed, sqlglot.exp.Select) else parsed.this
    output_names = [e.alias_or_name for e in outer_select.expressions]
    assert output_names[:2] == ["pickup.city", "dropoff.city"]
    assert len(set(output_names)) == len(output_names)


def test_percentile_postgres_no_fallback_warning(
    rg_resolver: ContractResolver,
    orders_rg: SemanticSource,
    customers_rg: SemanticSource,
) -> None:
    """Postgres (native PERCENTILE_CONT) never emits the fallback warning."""
    result = compile(
        SemanticQuery(metrics=["median_order_value"], dimensions=["region"]),
        rg_resolver,
        [orders_rg, customers_rg],
    )
    assert result.warnings == []


# ---------------------------------------------------------------------------
# AC3 (S2b-AC4) — population_filter applied before COUNT(DISTINCT …)
# ---------------------------------------------------------------------------


def test_ac3_population_filter_distinct_count_scalar(
    orders_rg: SemanticSource,
) -> None:
    """population_filter appears in WHERE before COUNT(DISTINCT …) at scalar grain."""
    binding = MetricBinding(
        metric="active_customers",
        canonical=CanonicalRef(
            kind=BindingKind.DISTINCT_COUNT,
            source="orders",
            distinct_on="customer_id",
            population_filter="customer_id NOT IN (SELECT customer_id FROM test_accounts)",
        ),
    )
    resolver = ContractResolver(bindings=[binding], guardrails=[])
    result = compile(
        SemanticQuery(metrics=["active_customers"]),
        resolver,
        [orders_rg],
    )
    _parse_ok(result.sql)
    sql_upper = result.sql.upper()
    assert "COUNT(DISTINCT" in sql_upper
    assert "TEST_ACCOUNTS" in sql_upper  # filter present before aggregation


def test_ac3_population_filter_distinct_count_grouped(
    orders_rg: SemanticSource,
) -> None:
    """population_filter applied at every requested grain (grouped by week)."""
    binding = MetricBinding(
        metric="active_customers",
        canonical=CanonicalRef(
            kind=BindingKind.DISTINCT_COUNT,
            source="orders",
            distinct_on="customer_id",
            population_filter="customer_id NOT IN (SELECT customer_id FROM test_accounts)",
        ),
    )
    resolver = ContractResolver(bindings=[binding], guardrails=[])
    result = compile(
        SemanticQuery(metrics=["active_customers"], dimensions=["week"]),
        resolver,
        [orders_rg],
    )
    _parse_ok(result.sql)
    sql_upper = result.sql.upper()
    assert "COUNT(DISTINCT" in sql_upper
    assert "TEST_ACCOUNTS" in sql_upper
    assert "GROUP BY" in sql_upper


# ---------------------------------------------------------------------------
# Fanout safety — distinct_count tolerates; percentile rejects
# ---------------------------------------------------------------------------


def test_distinct_count_tolerates_fanout_join(
    rg_resolver: ContractResolver,
    orders_rg: SemanticSource,
    order_items_rg: SemanticSource,
) -> None:
    """distinct_count + one_to_many join compiles — DISTINCT dedups row duplication."""
    result = compile(
        SemanticQuery(metrics=["active_customers"], dimensions=["sku"]),
        rg_resolver,
        [orders_rg, order_items_rg],
    )
    _parse_ok(result.sql)
    sql_upper = result.sql.upper()
    assert "COUNT(DISTINCT" in sql_upper


def test_percentile_rejects_fanout_join(
    rg_resolver: ContractResolver,
    orders_rg: SemanticSource,
    order_items_rg: SemanticSource,
) -> None:
    """percentile + one_to_many join → FANOUT_UNSAFE (row duplication corrupts quantile)."""
    with pytest.raises(exc.FanoutUnsafe):
        compile(
            SemanticQuery(metrics=["median_order_value"], dimensions=["sku"]),
            rg_resolver,
            [orders_rg, order_items_rg],
        )


def test_distinct_count_many_to_one_join_ok(
    rg_resolver: ContractResolver,
    orders_rg: SemanticSource,
    customers_rg: SemanticSource,
) -> None:
    """distinct_count + many_to_one join (customers) compiles and uses COUNT(DISTINCT …)."""
    result = compile(
        SemanticQuery(metrics=["active_customers"], dimensions=["region"]),
        rg_resolver,
        [orders_rg, customers_rg],
    )
    _parse_ok(result.sql)
    assert "COUNT(DISTINCT" in result.sql.upper()


# ---------------------------------------------------------------------------
# Multi-metric rejection
# ---------------------------------------------------------------------------


def test_recompute_at_grain_must_be_queried_alone(
    rg_resolver: ContractResolver,
    orders_rg: SemanticSource,
) -> None:
    """Querying a recompute_at_grain metric alongside another raises UnsupportedMeasure."""
    with pytest.raises(exc.UnsupportedMeasure):
        compile(
            SemanticQuery(metrics=["active_customers", "median_order_value"]),
            rg_resolver,
            [orders_rg],
        )


# ---------------------------------------------------------------------------
# Model validation — CanonicalRef shape errors
# ---------------------------------------------------------------------------


def test_distinct_count_requires_source() -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        CanonicalRef(kind=BindingKind.DISTINCT_COUNT, distinct_on="customer_id")


def test_distinct_count_requires_distinct_on() -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        CanonicalRef(kind=BindingKind.DISTINCT_COUNT, source="orders")


def test_percentile_requires_quantile_in_range() -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        CanonicalRef(kind=BindingKind.PERCENTILE, source="orders", column="amount", quantile=1.5)


def test_percentile_quantile_zero_rejected() -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        CanonicalRef(kind=BindingKind.PERCENTILE, source="orders", column="amount", quantile=0.0)
