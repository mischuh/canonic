"""Multi-metric simple/additive compile path — per-metric filter isolation.

Regression for a bug where compiling several SINGLE-kind metrics together (e.g.
``--metrics total_income,total_expenses,net_cashflow``) returned ``None`` for every
metric, while querying each one individually worked fine. Root cause: every requested
metric's ``population_filter`` and per-measure ``mandatory_filter`` guardrails were
folded into ONE shared WHERE clause covering the single flat SELECT that all metrics
share as sibling projections. When two metrics carry different (e.g. mutually
exclusive) restrictions, the combined WHERE can never match any row, so every
aggregate in that one result row comes back SQL NULL.

Fix: with more than one requested metric, each metric's own population_filter/
guardrail conditions scope only that metric's own aggregate via conditional
aggregation (``SUM(CASE WHEN <condition> THEN <expr> END)``), never a shared WHERE.
A single-metric query is emitted exactly as before (still a plain shared WHERE) —
see the "no-op" and "single-metric shape frozen" tests below.
"""

from __future__ import annotations

import duckdb
import pytest
import sqlglot

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
from canonic.exc import FanoutUnsafe, Unreachable
from canonic.semantic.models import Column, Dimension, Join, Measure, Relationship, SemanticSource


def _parse_ok(sql: str) -> None:
    sqlglot.parse_one(sql, dialect="postgres")


@pytest.fixture
def transactions() -> SemanticSource:
    """Fact table with a type column so population_filter can split income/expense."""
    return SemanticSource(
        name="transactions",
        connection="warehouse_duckdb",
        table="fct_transactions",
        grain=["txn_id"],
        columns=[
            Column(name="txn_id", type="string", nullable=False),
            Column(name="amount", type="decimal", nullable=True),
            Column(name="type", type="string", nullable=False),
        ],
        measures=[Measure(name="amount", expr="sum(amount)", additivity="additive")],
        dimensions=[],
    )


@pytest.fixture
def total_income_binding() -> MetricBinding:
    return MetricBinding(
        metric="total_income",
        canonical=CanonicalRef(
            source="transactions", measure="amount", population_filter="type = 'income'"
        ),
    )


@pytest.fixture
def total_expenses_binding() -> MetricBinding:
    return MetricBinding(
        metric="total_expenses",
        canonical=CanonicalRef(
            source="transactions", measure="amount", population_filter="type = 'expense'"
        ),
    )


@pytest.fixture
def net_cashflow_binding() -> MetricBinding:
    return MetricBinding(
        metric="net_cashflow",
        canonical=CanonicalRef(source="transactions", measure="amount"),
    )


@pytest.fixture
def resolver_multi(
    total_income_binding: MetricBinding,
    total_expenses_binding: MetricBinding,
    net_cashflow_binding: MetricBinding,
) -> ContractResolver:
    return ContractResolver(
        bindings=[total_income_binding, total_expenses_binding, net_cashflow_binding],
        guardrails=[],
    )


# ---------------------------------------------------------------------------
# Direct regression: two conflicting population_filters no longer collide
# ---------------------------------------------------------------------------


def test_two_conflicting_population_filters_dont_collide_structurally(
    resolver_multi: ContractResolver, transactions: SemanticSource
) -> None:
    """No shared WHERE; each metric's filter is scoped via its own CASE WHEN."""
    result = compile(
        SemanticQuery(metrics=["total_income", "total_expenses"]), resolver_multi, [transactions]
    )
    _parse_ok(result.sql)
    sql_upper = result.sql.upper()
    assert "WHERE" not in sql_upper
    assert sql_upper.count("CASE WHEN") == 2
    assert "'income'" in result.sql
    assert "'expense'" in result.sql


def test_population_filter_mixed_with_metric_without_filter(
    resolver_multi: ContractResolver, transactions: SemanticSource
) -> None:
    """Three metrics, only two carry a filter: exactly 2 CASE WHEN, third stays plain."""
    result = compile(
        SemanticQuery(metrics=["total_income", "total_expenses", "net_cashflow"]),
        resolver_multi,
        [transactions],
    )
    _parse_ok(result.sql)
    assert result.sql.upper().count("CASE WHEN") == 2
    assert result.sql.upper().count("SUM(") == 3


def test_multi_metric_execution_no_longer_returns_none(
    resolver_multi: ContractResolver, transactions: SemanticSource
) -> None:
    """The falsifying test: execute the compiled SQL against real data — no NULLs."""
    result = compile(
        SemanticQuery(metrics=["total_income", "total_expenses", "net_cashflow"]),
        resolver_multi,
        [transactions],
        connection_dialects={"warehouse_duckdb": "duckdb"},
    )
    con = duckdb.connect(":memory:")
    con.execute("CREATE TABLE fct_transactions (txn_id VARCHAR, amount DECIMAL, type VARCHAR)")
    con.execute(
        "INSERT INTO fct_transactions VALUES "
        "('1', 100, 'income'), ('2', 50, 'income'), ('3', -30, 'expense'), ('4', -20, 'expense')"
    )
    row = con.execute(result.sql).fetchone()
    assert row is not None
    total_income, total_expenses, net_cashflow = row
    assert None not in row
    assert total_income == 150
    assert total_expenses == -50
    assert net_cashflow == 100


# ---------------------------------------------------------------------------
# Guardrail mandatory_filter suffers (and is fixed for) the same collision
# ---------------------------------------------------------------------------


def test_guardrail_mandatory_filter_scoped_per_metric() -> None:
    """Two measures, each with a mutually exclusive mandatory_filter guardrail.

    Guardrails key on (source, measure), so each metric needs its own measure to
    keep the two guardrails independent.
    """
    expense_source = SemanticSource(
        name="transactions",
        connection="warehouse_duckdb",
        table="fct_transactions",
        grain=["txn_id"],
        columns=[
            Column(name="txn_id", type="string", nullable=False),
            Column(name="amount", type="decimal", nullable=True),
            Column(name="type", type="string", nullable=False),
        ],
        measures=[
            Measure(name="income_amount", expr="sum(amount)", additivity="additive"),
            Measure(name="expense_amount", expr="sum(amount)", additivity="additive"),
        ],
        dimensions=[],
    )
    income_binding = MetricBinding(
        metric="total_income",
        canonical=CanonicalRef(source="transactions", measure="income_amount"),
    )
    expense_binding = MetricBinding(
        metric="total_expenses",
        canonical=CanonicalRef(source="transactions", measure="expense_amount"),
    )
    income_guardrail = Guardrail(
        id="income-only",
        applies_to=AppliesTo(source="transactions", measure="income_amount"),
        kind=GuardrailKind.MANDATORY_FILTER,
        filter="type = 'income'",
        severity=Severity.ERROR,
        rationale="income_amount must only ever see income rows.",
    )
    expense_guardrail = Guardrail(
        id="expense-only",
        applies_to=AppliesTo(source="transactions", measure="expense_amount"),
        kind=GuardrailKind.MANDATORY_FILTER,
        filter="type = 'expense'",
        severity=Severity.ERROR,
        rationale="expense_amount must only ever see expense rows.",
    )
    resolver = ContractResolver(
        bindings=[income_binding, expense_binding],
        guardrails=[income_guardrail, expense_guardrail],
    )
    result = compile(
        SemanticQuery(metrics=["total_income", "total_expenses"]), resolver, [expense_source]
    )
    _parse_ok(result.sql)
    sql_upper = result.sql.upper()
    assert "WHERE" not in sql_upper
    assert sql_upper.count("CASE WHEN") == 2
    assert {g.id for g in result.guardrails_fired} == {"income-only", "expense-only"}


# ---------------------------------------------------------------------------
# No-op regression guard: no filters/guardrails at all → unchanged SQL shape
# ---------------------------------------------------------------------------


def test_no_filters_multi_metric_unchanged_shape(transactions: SemanticSource) -> None:
    """Two plain metrics with no population_filter/guardrails: no CASE WHEN, no WHERE."""
    b1 = MetricBinding(
        metric="metric_a", canonical=CanonicalRef(source="transactions", measure="amount")
    )
    b2 = MetricBinding(
        metric="metric_b", canonical=CanonicalRef(source="transactions", measure="amount")
    )
    resolver = ContractResolver(bindings=[b1, b2], guardrails=[])
    result = compile(SemanticQuery(metrics=["metric_a", "metric_b"]), resolver, [transactions])
    _parse_ok(result.sql)
    sql_upper = result.sql.upper()
    assert "CASE WHEN" not in sql_upper
    assert "WHERE" not in sql_upper
    assert sql_upper.count("SUM(") == 2


# ---------------------------------------------------------------------------
# Fanout / dedup path also gets per-metric conditional aggregation
# ---------------------------------------------------------------------------


@pytest.fixture
def transactions_with_fanout() -> SemanticSource:
    """Same transactions source, joined one_to_many to a child table to force dedup."""
    return SemanticSource(
        name="transactions",
        connection="warehouse_duckdb",
        table="fct_transactions",
        grain=["txn_id"],
        columns=[
            Column(name="txn_id", type="string", nullable=False),
            Column(name="amount", type="decimal", nullable=True),
            Column(name="type", type="string", nullable=False),
        ],
        measures=[Measure(name="amount", expr="sum(amount)", additivity="additive")],
        dimensions=[],
        joins=[
            Join(
                to="transaction_tags",
                on="transactions.txn_id = transaction_tags.txn_id",
                relationship=Relationship.ONE_TO_MANY,
            )
        ],
    )


@pytest.fixture
def transaction_tags() -> SemanticSource:
    return SemanticSource(
        name="transaction_tags",
        connection="warehouse_duckdb",
        table="fct_transaction_tags",
        grain=["tag_id"],
        columns=[
            Column(name="tag_id", type="string", nullable=False),
            Column(name="txn_id", type="string", nullable=False),
            Column(name="tag", type="string", nullable=False),
        ],
        dimensions=[Dimension(name="tag", column="tag")],
    )


def test_fanout_dedup_path_conditional_aggregation(
    resolver_multi: ContractResolver,
    transactions_with_fanout: SemanticSource,
    transaction_tags: SemanticSource,
) -> None:
    """A one_to_many join forces _build_deduped; per-metric filters still isolate correctly."""
    result = compile(
        SemanticQuery(metrics=["total_income", "total_expenses"], dimensions=["tag"]),
        resolver_multi,
        [transactions_with_fanout, transaction_tags],
    )
    _parse_ok(result.sql)
    sql_upper = result.sql.upper()
    assert "DISTINCT ON" in sql_upper
    assert sql_upper.count("CASE WHEN") == 2
    # The filter's source column must be projected by the inner dedup subquery.
    assert '"type"' in result.sql.lower() or "type" in result.sql.lower()


# ---------------------------------------------------------------------------
# Cross-source metrics: metrics bound to different (but joined) sources
#
# Regression for a bug where the join-planning target set (`referenced` in
# `_compile_simple_additive`) was built only from dimensions and filters, never from
# the source of any non-owner metric. A query combining e.g. `orders.revenue` and
# `order_items.units_sold` compiled to a SELECT that referenced `order_items` without
# ever joining it in, failing at execution time with a DuckDB BinderException.
#
# Fixing the missing join surfaced a second, more dangerous latent issue: once the
# join *is* planned, a one_to_many/many_to_many or many_to_one relationship between
# the two sources makes a flat-SELECT emission produce a *wrong* (not merely
# uncompilable) aggregate for the non-owner metric — confirmed by direct reproduction.
# Only one_to_one is safe for a single flat join.
#
# For any other relationship between two ADDITIVE metrics' sources, the compiler now
# aggregates each source independently at its own grain (one CTE per metric source,
# `_build_multi_source`/`_plan_metric_group` in `simple_additive.py`) and combines the
# fully-aggregated leaves with a `FULL JOIN USING (<dims>)` — a leaf that's already
# aggregated to one row per group can't be fanned out by the combining join, so this is
# correct regardless of the relationship. Non-additive/semi-additive metrics reached via
# a fanout edge still raise `FanoutUnsafe` — pre-aggregating those would mean invoking a
# different compile strategy per leaf, out of scope here.
# ---------------------------------------------------------------------------


@pytest.fixture
def orders() -> SemanticSource:
    return SemanticSource(
        name="orders",
        connection="warehouse_duckdb",
        table="fct_orders",
        grain=["order_id"],
        columns=[
            Column(name="order_id", type="string", nullable=False),
            Column(name="amount", type="decimal", nullable=True),
        ],
        measures=[Measure(name="revenue", expr="sum(amount)", additivity="additive")],
        dimensions=[],
        joins=[
            Join(
                to="order_details",
                on="orders.order_id = order_details.order_id",
                relationship=Relationship.ONE_TO_ONE,
            )
        ],
    )


@pytest.fixture
def order_details() -> SemanticSource:
    """A one_to_one companion to ``orders`` (e.g. one shipping record per order)."""
    return SemanticSource(
        name="order_details",
        connection="warehouse_duckdb",
        table="fct_order_details",
        grain=["order_id"],
        columns=[
            Column(name="order_id", type="string", nullable=False),
            Column(name="shipping_cost", type="decimal", nullable=True),
        ],
        measures=[Measure(name="shipping", expr="sum(shipping_cost)", additivity="additive")],
        dimensions=[],
    )


def test_cross_source_metrics_one_to_one_join_planned_and_correct(
    orders: SemanticSource, order_details: SemanticSource
) -> None:
    """Two metrics on two one_to_one-joined sources: the join is planned and the SQL is valid."""
    revenue_b = MetricBinding(
        metric="revenue", canonical=CanonicalRef(source="orders", measure="revenue")
    )
    shipping_b = MetricBinding(
        metric="shipping", canonical=CanonicalRef(source="order_details", measure="shipping")
    )
    resolver = ContractResolver(bindings=[revenue_b, shipping_b], guardrails=[])
    result = compile(
        SemanticQuery(metrics=["revenue", "shipping"]),
        resolver,
        [order_details, orders],
        connection_dialects={"warehouse_duckdb": "duckdb"},
    )
    _parse_ok(result.sql)
    assert "JOIN" in result.sql.upper()
    assert '"orders"' in result.sql

    con = duckdb.connect(":memory:")
    con.execute("CREATE TABLE fct_orders (order_id VARCHAR, amount DECIMAL)")
    con.execute("CREATE TABLE fct_order_details (order_id VARCHAR, shipping_cost DECIMAL)")
    con.execute("INSERT INTO fct_orders VALUES ('1', 100), ('2', 50)")
    con.execute("INSERT INTO fct_order_details VALUES ('1', 5), ('2', 3)")
    row = con.execute(result.sql).fetchone()
    assert row is not None
    revenue, shipping = row
    assert revenue == 150
    assert shipping == 8


def test_cross_source_metrics_no_join_path_raises_unreachable(
    orders: SemanticSource, transactions: SemanticSource
) -> None:
    """Two metrics on sources with no declared join between them: compile-time error, not broken SQL."""
    revenue_b = MetricBinding(
        metric="revenue", canonical=CanonicalRef(source="orders", measure="revenue")
    )
    amount_b = MetricBinding(
        metric="txn_amount", canonical=CanonicalRef(source="transactions", measure="amount")
    )
    resolver = ContractResolver(bindings=[revenue_b, amount_b], guardrails=[])
    with pytest.raises(Unreachable):
        compile(SemanticQuery(metrics=["revenue", "txn_amount"]), resolver, [orders, transactions])


@pytest.fixture
def order_items() -> SemanticSource:
    return SemanticSource(
        name="order_items",
        connection="warehouse_duckdb",
        table="fct_order_items",
        grain=["item_id"],
        columns=[
            Column(name="item_id", type="string", nullable=False),
            Column(name="order_id", type="string", nullable=False),
            Column(name="quantity", type="decimal", nullable=True),
        ],
        measures=[Measure(name="units_sold", expr="sum(quantity)", additivity="additive")],
        dimensions=[],
    )


@pytest.mark.parametrize(
    "relationship",
    [Relationship.ONE_TO_MANY, Relationship.MANY_TO_ONE, Relationship.MANY_TO_MANY],
)
def test_cross_source_metrics_non_one_to_one_join_aggregates_leaves_correctly(
    orders: SemanticSource, order_items: SemanticSource, relationship: Relationship
) -> None:
    """A non-one_to_one join between two additive metrics' sources is aggregated per-leaf.

    A flat single join would undercount the many-side metric (one_to_many: owner-grain
    dedup collapses its rows) or overcount the one-side metric (many_to_one: its value is
    broadcast across every matching owner row) — confirmed by direct reproduction against
    real DuckDB data. Instead each source is aggregated independently at its own grain
    (one CTE per source) and the fully-aggregated leaves are combined, so the relationship
    between the sources can't corrupt either aggregate.
    """
    orders_with_join = orders.model_copy(
        update={
            "joins": [
                Join(
                    to="order_items",
                    on="orders.order_id = order_items.order_id",
                    relationship=relationship,
                )
            ]
        }
    )
    revenue_b = MetricBinding(
        metric="revenue", canonical=CanonicalRef(source="orders", measure="revenue")
    )
    units_b = MetricBinding(
        metric="units_sold", canonical=CanonicalRef(source="order_items", measure="units_sold")
    )
    resolver = ContractResolver(bindings=[revenue_b, units_b], guardrails=[])
    result = compile(
        SemanticQuery(metrics=["revenue", "units_sold"]),
        resolver,
        [orders_with_join, order_items],
        connection_dialects={"warehouse_duckdb": "duckdb"},
    )
    _parse_ok(result.sql)
    assert "WITH" in result.sql.upper()

    con = duckdb.connect(":memory:")
    con.execute("CREATE TABLE fct_orders (order_id VARCHAR, amount DECIMAL)")
    con.execute(
        "CREATE TABLE fct_order_items (item_id VARCHAR, order_id VARCHAR, quantity DECIMAL)"
    )
    con.execute("INSERT INTO fct_orders VALUES ('1', 100), ('2', 50)")
    con.execute(
        "INSERT INTO fct_order_items VALUES "
        "('a', '1', 2), ('b', '1', 3), ('c', '1', 4), ('d', '2', 5)"
    )
    row = con.execute(result.sql).fetchone()
    assert row is not None
    revenue, units_sold = row
    assert revenue == 150
    assert units_sold == 14


def test_cross_source_metrics_grouped_by_source_one_leaf_per_source(
    orders: SemanticSource, order_items: SemanticSource
) -> None:
    """Two metrics sharing one non-owner source are combined into a single leaf CTE."""
    order_items_with_second_measure = order_items.model_copy(
        update={
            "measures": [
                *order_items.measures,
                Measure(name="item_count", expr="count(item_id)", additivity="additive"),
            ]
        }
    )
    orders_with_join = orders.model_copy(
        update={
            "joins": [
                Join(
                    to="order_items",
                    on="orders.order_id = order_items.order_id",
                    relationship=Relationship.ONE_TO_MANY,
                )
            ]
        }
    )
    revenue_b = MetricBinding(
        metric="revenue", canonical=CanonicalRef(source="orders", measure="revenue")
    )
    units_b = MetricBinding(
        metric="units_sold", canonical=CanonicalRef(source="order_items", measure="units_sold")
    )
    count_b = MetricBinding(
        metric="item_count", canonical=CanonicalRef(source="order_items", measure="item_count")
    )
    resolver = ContractResolver(bindings=[revenue_b, units_b, count_b], guardrails=[])
    result = compile(
        SemanticQuery(metrics=["revenue", "units_sold", "item_count"]),
        resolver,
        [orders_with_join, order_items_with_second_measure],
        connection_dialects={"warehouse_duckdb": "duckdb"},
    )
    _parse_ok(result.sql)
    # Two metric sources total -> exactly two leaf CTEs, not three.
    assert result.sql.count("_leaf0") > 0
    assert result.sql.count("_leaf1") > 0
    assert "_leaf2" not in result.sql

    con = duckdb.connect(":memory:")
    con.execute("CREATE TABLE fct_orders (order_id VARCHAR, amount DECIMAL)")
    con.execute(
        "CREATE TABLE fct_order_items (item_id VARCHAR, order_id VARCHAR, quantity DECIMAL)"
    )
    con.execute("INSERT INTO fct_orders VALUES ('1', 100), ('2', 50)")
    con.execute(
        "INSERT INTO fct_order_items VALUES "
        "('a', '1', 2), ('b', '1', 3), ('c', '1', 4), ('d', '2', 5)"
    )
    row = con.execute(result.sql).fetchone()
    assert row is not None
    revenue, units_sold, item_count = row
    assert revenue == 150
    assert units_sold == 14
    assert item_count == 4


def test_cross_source_metrics_dimension_unreachable_from_one_leaf_raises_unreachable(
    orders: SemanticSource, order_items: SemanticSource
) -> None:
    """A dimension reachable from only one metric's source fails loud, not silently.

    Joins are declared one-directionally (SPEC §10 — never invented/reversed), so a
    dimension declared only on ``orders`` genuinely can't be projected from a leaf rooted
    at ``order_items`` unless a join back is explicitly declared.
    """
    orders_with_dim = orders.model_copy(
        update={
            "columns": [*orders.columns, Column(name="region", type="string", nullable=True)],
            "dimensions": [Dimension(name="region", column="region")],
            "joins": [
                Join(
                    to="order_items",
                    on="orders.order_id = order_items.order_id",
                    relationship=Relationship.ONE_TO_MANY,
                )
            ],
        }
    )
    revenue_b = MetricBinding(
        metric="revenue", canonical=CanonicalRef(source="orders", measure="revenue")
    )
    units_b = MetricBinding(
        metric="units_sold", canonical=CanonicalRef(source="order_items", measure="units_sold")
    )
    resolver = ContractResolver(bindings=[revenue_b, units_b], guardrails=[])
    with pytest.raises(Unreachable):
        compile(
            SemanticQuery(metrics=["revenue", "units_sold"], dimensions=["region"]),
            resolver,
            [orders_with_dim, order_items],
        )


def test_cross_source_metrics_non_additive_fanout_still_raises_fanout_unsafe(
    orders: SemanticSource,
) -> None:
    """The per-leaf pre-aggregation fix is additive-only: non-additive fanout still fails loud.

    Pre-aggregating a non-additive/semi-additive measure per leaf would mean invoking a
    different compile strategy per leaf (semi_additive.py/recompute.py) — out of scope for
    this fix, so the existing safety floor is unchanged.
    """
    order_items = SemanticSource(
        name="order_items",
        connection="warehouse_duckdb",
        table="fct_order_items",
        grain=["item_id"],
        columns=[
            Column(name="item_id", type="string", nullable=False),
            Column(name="order_id", type="string", nullable=False),
            Column(name="quantity", type="decimal", nullable=True),
        ],
        measures=[
            Measure(
                name="distinct_items", expr="count(distinct item_id)", additivity="non_additive"
            )
        ],
        dimensions=[],
    )
    orders_with_join = orders.model_copy(
        update={
            "joins": [
                Join(
                    to="order_items",
                    on="orders.order_id = order_items.order_id",
                    relationship=Relationship.ONE_TO_MANY,
                )
            ]
        }
    )
    revenue_b = MetricBinding(
        metric="revenue", canonical=CanonicalRef(source="orders", measure="revenue")
    )
    distinct_b = MetricBinding(
        metric="distinct_items",
        canonical=CanonicalRef(source="order_items", measure="distinct_items"),
    )
    resolver = ContractResolver(bindings=[revenue_b, distinct_b], guardrails=[])
    with pytest.raises(FanoutUnsafe):
        compile(
            SemanticQuery(metrics=["revenue", "distinct_items"]),
            resolver,
            [orders_with_join, order_items],
        )
