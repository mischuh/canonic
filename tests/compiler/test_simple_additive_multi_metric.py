"""Multi-metric simple/additive compile path — per-metric filter isolation.

Regression for a bug where compiling several SINGLE-kind metrics together (e.g.
``--metrics total_income,total_expenses,net_cashflow``) returned ``None`` for every
metric, while querying each one individually worked fine. Root cause: every requested
metric's ``population_filter`` and per-measure ``mandatory_filter`` guardrails were
folded into ONE shared WHERE clause covering the single flat SELECT that all metrics
share as sibling projections. When two metrics carry different (e.g. mutually
exclusive) restrictions, the combined WHERE can never match any row, so every
aggregate in that one result row comes back SQL NULL.

The first fix scoped each metric's conditions to its own aggregate via conditional
aggregation (``SUM(CASE WHEN <condition> THEN <expr> END)``). That was correct about
which rows fed which metric, but it kept every metric on one SELECT, and so inherited a
second-order wrong answer: a group that only a *sibling* metric has rows in still appears,
and the conditionally-aggregated metric reports a measured ``0`` for it rather than
"not applicable here".

AMENDMENT-multi-metric-compose replaces the mechanism rather than patching it. Each
metric is planned as its own leaf with its own honest WHERE, and compose fuses back
together only those leaves whose plans are genuinely identical. Metrics with different
populations therefore land on different leaves and are joined over a grain spine, where
an absent group reads NULL — which is what it means. So the tests below assert separate
leaves rather than ``CASE WHEN``, and the execution tests, which are the real regression
guards, are unchanged.
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
from canonic.exc import Unreachable
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
def transactions_two_measures(transactions: SemanticSource) -> SemanticSource:
    """The same source with a second, distinct measure, so two metrics can share a plan."""
    return transactions.model_copy(
        update={
            "measures": [
                *transactions.measures,
                Measure(name="txn_count", expr="count(*)", additivity="additive"),
            ]
        }
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
    """Different populations are different plans: one leaf each, each with its own WHERE."""
    result = compile(
        SemanticQuery(metrics=["total_income", "total_expenses"]), resolver_multi, [transactions]
    )
    _parse_ok(result.sql)
    sql_upper = result.sql.upper()
    assert sql_upper.count("_LEAF_") > 0
    assert "_LEAF_1" in sql_upper, "mutually exclusive populations must not share a leaf"
    assert sql_upper.count("WHERE") == 2, "one honest WHERE per population"
    assert "CASE WHEN" not in sql_upper
    assert "'income'" in result.sql
    assert "'expense'" in result.sql


def test_population_filter_mixed_with_metric_without_filter(
    resolver_multi: ContractResolver, transactions: SemanticSource
) -> None:
    """Three metrics, only two carry a filter: three distinct plans, two of them filtered."""
    result = compile(
        SemanticQuery(metrics=["total_income", "total_expenses", "net_cashflow"]),
        resolver_multi,
        [transactions],
    )
    _parse_ok(result.sql)
    sql_upper = result.sql.upper()
    assert "CASE WHEN" not in sql_upper
    assert sql_upper.count("SUM(") == 3
    assert "_LEAF_2" in sql_upper, "three populations, three leaves"
    assert sql_upper.count("WHERE") == 2, "the unfiltered metric carries no WHERE"


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
    assert "CASE WHEN" not in sql_upper
    assert sql_upper.count("WHERE") == 2, "each guardrail scopes its own leaf"
    assert "_LEAF_1" in sql_upper
    assert {g.id for g in result.guardrails_fired} == {"income-only", "expense-only"}


# ---------------------------------------------------------------------------
# No-op regression guard: no filters/guardrails at all → unchanged SQL shape
# ---------------------------------------------------------------------------


def test_two_metrics_on_the_same_measure_aggregate_once(transactions: SemanticSource) -> None:
    """Two metrics resolving to one measure with no filters are literally the same query.

    Same source, same measure, same (empty) filters, so both the leaf plan *and* the
    projected column are identical and the aggregate is computed once and referenced
    twice. This is leaf dedup proper, as opposed to fusion, which merges different
    measures that share a plan.
    """
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
    assert sql_upper.count("SUM(") == 1, "one aggregate, referenced by both metrics"
    assert "_LEAF_1" not in sql_upper
    # The caller asked for two metrics and gets two output columns.
    assert result.sql.count('"amount" AS "amount"') == 2


def test_two_distinct_measures_no_filters_fuse_to_one_flat_select(
    transactions_two_measures: SemanticSource,
) -> None:
    """Two different measures with identical plans fuse into one CTE-free flat SELECT.

    This is the shape that must not regress: the most common multi-metric query in the
    product stays a single GROUP BY over a single scan, exactly as before the amendment.
    """
    b1 = MetricBinding(
        metric="metric_a", canonical=CanonicalRef(source="transactions", measure="amount")
    )
    b2 = MetricBinding(
        metric="metric_b", canonical=CanonicalRef(source="transactions", measure="txn_count")
    )
    resolver = ContractResolver(bindings=[b1, b2], guardrails=[])
    result = compile(
        SemanticQuery(metrics=["metric_a", "metric_b"]), resolver, [transactions_two_measures]
    )
    _parse_ok(result.sql)
    sql_upper = result.sql.upper()
    assert "WITH" not in sql_upper, "a single fused leaf needs no CTE at all"
    assert "CASE WHEN" not in sql_upper
    assert "WHERE" not in sql_upper
    assert sql_upper.count("SUM(") == 1
    assert sql_upper.count("COUNT(") == 1


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


def test_fanout_dedup_applies_per_leaf(
    resolver_multi: ContractResolver,
    transactions_with_fanout: SemanticSource,
    transaction_tags: SemanticSource,
) -> None:
    """A one_to_many join forces the dedup shape, once per leaf, filters still isolated."""
    result = compile(
        SemanticQuery(metrics=["total_income", "total_expenses"], dimensions=["tag"]),
        resolver_multi,
        [transactions_with_fanout, transaction_tags],
    )
    _parse_ok(result.sql)
    sql_upper = result.sql.upper()
    assert sql_upper.count("DISTINCT ON") == 2, "each population dedups its own grain"
    assert "CASE WHEN" not in sql_upper
    assert sql_upper.count("WHERE") == 2
    assert "'income'" in result.sql
    assert "'expense'" in result.sql


# ---------------------------------------------------------------------------
# Cross-source metrics: metrics bound to different (but joined) sources
#
# Regression for a bug where the join-planning target set was built only from dimensions
# and filters, never from the source of any non-owner metric. A query combining e.g.
# `orders.revenue` and `order_items.units_sold` compiled to a SELECT that referenced
# `order_items` without ever joining it in, failing at execution with a BinderException.
#
# Fixing the missing join surfaced a second, more dangerous latent issue: once the join
# *is* planned, a one_to_many/many_to_many or many_to_one relationship between the two
# sources makes a flat-SELECT emission produce a *wrong* (not merely uncompilable)
# aggregate for the non-owner metric — confirmed by direct reproduction. Only one_to_one
# was ever safe for a single flat join, and telling those cases apart needed a
# hand-maintained relationship analysis that had to stay right forever.
#
# AMENDMENT-multi-metric-compose deleted that whole class of risk rather than maintaining
# the analysis: every metric is aggregated at its own grain in its own leaf before any
# cross-source join happens, so a leaf that is already one row per group cannot be fanned
# out by the join that combines it. There is no longer a relationship between two sources
# that a flat plan could get wrong, because there is no flat plan spanning two sources.
# The tests below therefore assert correct *values* per relationship rather than which
# emission strategy was picked.
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


def test_cross_source_metrics_with_no_join_path_still_compose(
    orders: SemanticSource, transactions: SemanticSource
) -> None:
    """Two metrics on entirely unrelated sources combine into one scalar row.

    This used to raise ``Unreachable``, because both metrics had to share one FROM clause
    and there was no join to build it from. Under the amendment each metric is aggregated
    independently and the results are set beside each other, so there is nothing to reach:
    total revenue next to total headcount is a legitimate question, not a join error
    (AMENDMENT §2 — the cross-metric constraint is reachability of *dimensions*, not of
    sources). ``test_..._dimension_unreachable_from_one_leaf_raises_unreachable`` below
    pins the constraint that does still apply.
    """
    revenue_b = MetricBinding(
        metric="revenue", canonical=CanonicalRef(source="orders", measure="revenue")
    )
    amount_b = MetricBinding(
        metric="txn_amount", canonical=CanonicalRef(source="transactions", measure="amount")
    )
    resolver = ContractResolver(bindings=[revenue_b, amount_b], guardrails=[])
    result = compile(
        SemanticQuery(metrics=["revenue", "txn_amount"]), resolver, [orders, transactions]
    )
    _parse_ok(result.sql)
    sql_upper = result.sql.upper()
    assert "CROSS JOIN" in sql_upper, "two one-row leaves, no grain to align"
    assert "_LEAF_1" in sql_upper


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
    # Three metrics, but only two distinct plans: the two order_items measures share a
    # source, dimensions and filters, so they fuse into one CTE projecting both.
    assert "_leaf_0" in result.sql
    assert "_leaf_1" in result.sql
    assert "_leaf_2" not in result.sql

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


def test_non_additive_metric_on_a_fanning_source_is_served_at_its_own_grain(
    orders: SemanticSource,
) -> None:
    """A non-additive metric next to a metric on a fanning source is safe, not FanoutUnsafe.

    This used to raise: both metrics shared one FROM clause, so counting distinct items
    meant traversing the one_to_many join that multiplies rows, and the safety floor
    correctly refused. Under the amendment ``distinct_items`` is aggregated on
    ``order_items`` alone, at its native grain, where no fanning join exists — so there is
    nothing to refuse. Fanout is a per-leaf question, and this leaf has none
    (AMENDMENT §2). The falsifying check is the executed value: 4 distinct items, not 3
    or 8.
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
    result = compile(
        SemanticQuery(metrics=["revenue", "distinct_items"]),
        resolver,
        [orders_with_join, order_items],
        connection_dialects={"warehouse_duckdb": "duckdb"},
    )
    _parse_ok(result.sql)
    assert "DISTINCT ON" not in result.sql.upper(), "no fanning join, so nothing to dedup"

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
    assert row == (150, 4)
