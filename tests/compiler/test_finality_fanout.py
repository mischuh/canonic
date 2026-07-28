"""Fanout regression for the finality UNION path (SPEC §4 step 4 x §7 stage 5).

Follow-up to the composite-metric fanout fix (jaffle_shop bug report): a finality-gated
metric's UNION branch is built per-realization, and each realization plans its own join
graph to the requested dimensions independently — one realization's topology can fan out
even when the query's outer owner topology (already fanout-checked before routing into
the finality path) does not. ``_build_finality_union`` previously built every branch as a
flat ``GROUP BY`` with no dedup and no safety floor, so an additive measure could be
silently inflated by a fanning join on just one realization, and a non-additive measure
could slip past the safety floor entirely if only a non-owner realization fanned out.
"""

from __future__ import annotations

from datetime import UTC, datetime

import duckdb
import pytest
import sqlglot

from canonic import exc
from canonic.compiler import SemanticQuery, compile
from canonic.contracts.models import (
    BindingKind,
    CanonicalRef,
    FinalityRule,
    MetricBinding,
    Realization,
)
from canonic.contracts.resolver import ContractResolver
from canonic.semantic.models import Column, Dimension, Join, Measure, Relationship, SemanticSource

_AS_OF = datetime(2026, 6, 13, 10, 0, 0, tzinfo=UTC)


def _parse_ok_union(sql: str) -> None:
    parsed = sqlglot.parse_one(sql, dialect="postgres")
    assert isinstance(parsed, (sqlglot.exp.Select, sqlglot.exp.Union))


# ---------------------------------------------------------------------------
# Fixtures — additive revenue metric, fanning join on BOTH realizations
# ---------------------------------------------------------------------------


@pytest.fixture
def orders_final() -> SemanticSource:
    """Final realization: fans out to order_items_final to reach 'sku'."""
    return SemanticSource(
        name="orders_final",
        connection="warehouse_pg",
        table="fct_orders",
        grain=["order_id"],
        columns=[
            Column(name="order_id", type="string", nullable=False),
            Column(name="amount", type="decimal", nullable=False),
            Column(name="created_at", type="timestamp", nullable=False),
        ],
        measures=[Measure(name="total_revenue", expr="sum(amount)", additivity="additive")],
        dimensions=[Dimension(name="order_date", column="created_at", granularity="day")],
        joins=[
            Join(
                to="order_items_final",
                on="orders_final.order_id = order_items_final.order_id",
                relationship=Relationship.ONE_TO_MANY,
            ),
        ],
    )


@pytest.fixture
def order_items_final() -> SemanticSource:
    return SemanticSource(
        name="order_items_final",
        connection="warehouse_pg",
        table="fct_order_items",
        grain=["item_id"],
        columns=[
            Column(name="item_id", type="string", nullable=False),
            Column(name="order_id", type="string", nullable=False),
            Column(name="sku", type="string", nullable=False),
        ],
        dimensions=[Dimension(name="sku", column="sku")],
    )


@pytest.fixture
def orders_rt() -> SemanticSource:
    """Provisional realization: same schema, also fans out to reach 'sku'."""
    return SemanticSource(
        name="orders_rt",
        connection="warehouse_pg",
        table="fct_orders_rt",
        grain=["order_id"],
        columns=[
            Column(name="order_id", type="string", nullable=False),
            Column(name="amount", type="decimal", nullable=False),
            Column(name="created_at", type="timestamp", nullable=False),
        ],
        measures=[Measure(name="total_revenue", expr="sum(amount)", additivity="additive")],
        dimensions=[Dimension(name="order_date", column="created_at", granularity="day")],
        joins=[
            Join(
                to="order_items_rt",
                on="orders_rt.order_id = order_items_rt.order_id",
                relationship=Relationship.ONE_TO_MANY,
            ),
        ],
    )


@pytest.fixture
def order_items_rt() -> SemanticSource:
    return SemanticSource(
        name="order_items_rt",
        connection="warehouse_pg",
        table="fct_order_items_rt",
        grain=["item_id"],
        columns=[
            Column(name="item_id", type="string", nullable=False),
            Column(name="order_id", type="string", nullable=False),
            Column(name="sku", type="string", nullable=False),
        ],
        dimensions=[Dimension(name="sku", column="sku")],
    )


@pytest.fixture
def sources(
    orders_final: SemanticSource,
    order_items_final: SemanticSource,
    orders_rt: SemanticSource,
    order_items_rt: SemanticSource,
) -> list[SemanticSource]:
    return [orders_final, order_items_final, orders_rt, order_items_rt]


@pytest.fixture
def revenue_binding() -> MetricBinding:
    return MetricBinding(
        metric="revenue",
        canonical=CanonicalRef(source="orders_final", measure="total_revenue"),
    )


@pytest.fixture
def revenue_finality_rule() -> FinalityRule:
    return FinalityRule(
        metric="revenue",
        realizations=[
            Realization(
                source="orders_final",
                role="final",
                watermark="business_day - 1 day",
                tz="America/New_York",
            ),
            Realization(source="orders_rt", role="provisional"),
        ],
        coalescing="window <= watermark ? final : provisional",
        result_flag="per_row",
    )


@pytest.fixture
def resolver(
    revenue_binding: MetricBinding, revenue_finality_rule: FinalityRule
) -> ContractResolver:
    return ContractResolver(
        bindings=[revenue_binding], guardrails=[], finality=[revenue_finality_rule]
    )


# ---------------------------------------------------------------------------
# Additive + fanout on both branches: dedup, don't inflate
# ---------------------------------------------------------------------------


def test_fanout_finality_both_branches_dedup(
    resolver: ContractResolver, sources: list[SemanticSource]
) -> None:
    """Grouping a finality-gated additive metric by a fanning dimension: both UNION
    branches dedup independently, since each realization plans its own join graph."""
    result = compile(
        SemanticQuery(metrics=["revenue"], dimensions=["order_date", "sku"], as_of=_AS_OF),
        resolver,
        sources,
    )
    _parse_ok_union(result.sql)
    sql_upper = result.sql.upper()
    assert "UNION ALL" in sql_upper
    assert sql_upper.count("DISTINCT ON") == 2


def test_fanout_finality_numeric_correctness(
    resolver: ContractResolver, sources: list[SemanticSource]
) -> None:
    """Executed regression: a multi-line-item order in the final realization must not
    inflate revenue. Correct: 100 (o1) + 40 (o2) = 140 for sku 'X'. The un-deduped bug
    fans o1's row out over its two line items and sums 100+100+40=240 instead.
    """
    result = compile(
        SemanticQuery(metrics=["revenue"], dimensions=["order_date", "sku"], as_of=_AS_OF),
        resolver,
        sources,
        connection_dialects={"warehouse_pg": "duckdb"},
    )
    # Plain TIMESTAMP (not TIMESTAMPTZ): DuckDB's DATE_TRUNC on TIMESTAMPTZ requires the
    # ICU extension's tz database (pytz), which isn't guaranteed to be present in this
    # environment; the compiler's own emitted CAST(... AS TIMESTAMPTZ) watermark literal
    # still compares fine against a plain TIMESTAMP column via implicit cast.
    con = duckdb.connect(":memory:")
    con.execute("CREATE TABLE fct_orders (order_id VARCHAR, amount DECIMAL, created_at TIMESTAMP)")
    con.execute("CREATE TABLE fct_order_items (item_id VARCHAR, order_id VARCHAR, sku VARCHAR)")
    con.execute(
        "CREATE TABLE fct_orders_rt (order_id VARCHAR, amount DECIMAL, created_at TIMESTAMP)"
    )
    con.execute("CREATE TABLE fct_order_items_rt (item_id VARCHAR, order_id VARCHAR, sku VARCHAR)")
    con.execute(
        "INSERT INTO fct_orders VALUES "
        "('o1', 100, '2026-06-01T00:00:00'), ('o2', 40, '2026-06-01T00:00:00')"
    )
    con.execute(
        "INSERT INTO fct_order_items VALUES ('i1', 'o1', 'X'), ('i2', 'o1', 'X'), ('i3', 'o2', 'X')"
    )
    row = con.execute(result.sql).fetchone()
    assert row is not None
    _order_date, sku, revenue, is_final = row
    assert sku == "X"
    assert revenue == pytest.approx(140.0)
    assert is_final is True


# ---------------------------------------------------------------------------
# Safety floor must also apply per branch, not just the outer owner topology
# ---------------------------------------------------------------------------


def test_fanout_finality_non_additive_realization_only_raises() -> None:
    """The outer owner (orders_final) reaches 'sku' natively — no fanning join, so the
    existing global safety-floor check (computed against the outer owner's own topology)
    lets a non-additive measure through. But the provisional realization (orders_rt) only
    reaches 'sku' via a one_to_many join to order_items_rt — that branch alone must still
    be rejected as FanoutUnsafe rather than silently corrupting the aggregate.
    """
    orders_final = SemanticSource(
        name="orders_final",
        connection="warehouse_pg",
        table="fct_orders",
        grain=["order_id"],
        columns=[
            Column(name="order_id", type="string", nullable=False),
            Column(name="sku", type="string", nullable=False),
            Column(name="created_at", type="timestamp", nullable=False),
        ],
        measures=[
            Measure(
                name="distinct_orders", expr="count(distinct order_id)", additivity="non_additive"
            ),
        ],
        dimensions=[
            Dimension(name="order_date", column="created_at", granularity="day"),
            Dimension(name="sku", column="sku"),
        ],
    )
    order_items_rt = SemanticSource(
        name="order_items_rt",
        connection="warehouse_pg",
        table="fct_order_items_rt",
        grain=["item_id"],
        columns=[
            Column(name="item_id", type="string", nullable=False),
            Column(name="order_id", type="string", nullable=False),
            Column(name="sku", type="string", nullable=False),
        ],
        dimensions=[Dimension(name="sku", column="sku")],
    )
    orders_rt = SemanticSource(
        name="orders_rt",
        connection="warehouse_pg",
        table="fct_orders_rt",
        grain=["order_id"],
        columns=[
            Column(name="order_id", type="string", nullable=False),
            Column(name="created_at", type="timestamp", nullable=False),
        ],
        measures=[
            Measure(
                name="distinct_orders", expr="count(distinct order_id)", additivity="non_additive"
            ),
        ],
        dimensions=[Dimension(name="order_date", column="created_at", granularity="day")],
        joins=[
            Join(
                to="order_items_rt",
                on="orders_rt.order_id = order_items_rt.order_id",
                relationship=Relationship.ONE_TO_MANY,
            ),
        ],
    )
    binding = MetricBinding(
        metric="distinct_order_count",
        canonical=CanonicalRef(source="orders_final", measure="distinct_orders"),
    )
    rule = FinalityRule(
        metric="distinct_order_count",
        realizations=[
            Realization(
                source="orders_final",
                role="final",
                watermark="business_day - 1 day",
                tz="America/New_York",
            ),
            Realization(source="orders_rt", role="provisional"),
        ],
        coalescing="window <= watermark ? final : provisional",
        result_flag="per_row",
    )
    resolver = ContractResolver(bindings=[binding], guardrails=[], finality=[rule])
    sources = [orders_final, orders_rt, order_items_rt]

    with pytest.raises(exc.FanoutUnsafe):
        compile(
            SemanticQuery(
                metrics=["distinct_order_count"], dimensions=["order_date", "sku"], as_of=_AS_OF
            ),
            resolver,
            sources,
        )


# ---------------------------------------------------------------------------
# composite.py benefits automatically — the fix lives in the shared
# _build_finality_union helper, not in composite.py or simple_additive.py.
# ---------------------------------------------------------------------------


def test_fanout_finality_composite_leaf_dedups_both_branches() -> None:
    """A ratio metric whose denominator has a finality rule AND needs a fanning join to
    reach the requested dimension: composite.py's _plan_leaf calls the same shared
    _build_finality_union, so its leaf's UNION branches must dedup too, without any
    change to composite.py itself.

    Expect 3 DISTINCT ON: 1 from the numerator leaf (additive + fanout, no finality —
    already fixed by the prior composite-metric fix) and 2 from the denominator leaf's
    final/provisional UNION branches (this fix).
    """
    damages = SemanticSource(
        name="damages",
        connection="warehouse_pg",
        table="fct_damages",
        grain=["damage_id"],
        columns=[
            Column(name="damage_id", type="string", nullable=False),
            Column(name="repair_cost", type="decimal", nullable=True),
            Column(name="reported_at", type="timestamp", nullable=False),
        ],
        measures=[
            Measure(name="total_repair_cost", expr="sum(repair_cost)", additivity="additive"),
            Measure(name="damage_count", expr="count(damage_id)", additivity="additive"),
        ],
        dimensions=[Dimension(name="report_date", column="reported_at", granularity="day")],
        joins=[
            Join(
                to="repair_line_items",
                on="damages.damage_id = repair_line_items.damage_id",
                relationship=Relationship.ONE_TO_MANY,
            ),
        ],
    )
    repair_line_items = SemanticSource(
        name="repair_line_items",
        connection="warehouse_pg",
        table="fct_repair_line_items",
        grain=["line_item_id"],
        columns=[
            Column(name="line_item_id", type="string", nullable=False),
            Column(name="damage_id", type="string", nullable=False),
            Column(name="part_name", type="string", nullable=False),
        ],
        dimensions=[Dimension(name="part_name", column="part_name")],
    )
    damages_rt = SemanticSource(
        name="damages_rt",
        connection="warehouse_pg",
        table="fct_damages_rt",
        grain=["damage_id"],
        columns=[
            Column(name="damage_id", type="string", nullable=False),
            Column(name="repair_cost", type="decimal", nullable=True),
            Column(name="reported_at", type="timestamp", nullable=False),
        ],
        measures=[
            Measure(name="total_repair_cost", expr="sum(repair_cost)", additivity="additive"),
            Measure(name="damage_count", expr="count(damage_id)", additivity="additive"),
        ],
        dimensions=[Dimension(name="report_date", column="reported_at", granularity="day")],
        joins=[
            Join(
                to="repair_line_items_rt",
                on="damages_rt.damage_id = repair_line_items_rt.damage_id",
                relationship=Relationship.ONE_TO_MANY,
            ),
        ],
    )
    repair_line_items_rt = SemanticSource(
        name="repair_line_items_rt",
        connection="warehouse_pg",
        table="fct_repair_line_items_rt",
        grain=["line_item_id"],
        columns=[
            Column(name="line_item_id", type="string", nullable=False),
            Column(name="damage_id", type="string", nullable=False),
            Column(name="part_name", type="string", nullable=False),
        ],
        dimensions=[Dimension(name="part_name", column="part_name")],
    )

    total_cost_binding = MetricBinding(
        metric="total_repair_cost",
        canonical=CanonicalRef(source="damages", measure="total_repair_cost"),
    )
    damage_count_binding = MetricBinding(
        metric="damage_count",
        canonical=CanonicalRef(source="damages", measure="damage_count"),
    )
    avg_costs_binding = MetricBinding(
        metric="avg_repair_costs",
        canonical=CanonicalRef(
            kind=BindingKind.RATIO,
            numerator="total_repair_cost",
            denominator="damage_count",
        ),
    )
    denominator_finality_rule = FinalityRule(
        metric="damage_count",
        realizations=[
            Realization(source="damages", role="final", watermark="business_day - 1 day", tz="UTC"),
            Realization(source="damages_rt", role="provisional"),
        ],
        coalescing="window <= watermark ? final : provisional",
        result_flag="per_row",
    )
    resolver = ContractResolver(
        bindings=[avg_costs_binding, total_cost_binding, damage_count_binding],
        guardrails=[],
        finality=[denominator_finality_rule],
    )
    sources = [damages, repair_line_items, damages_rt, repair_line_items_rt]

    result = compile(
        SemanticQuery(
            metrics=["avg_repair_costs"], dimensions=["report_date", "part_name"], as_of=_AS_OF
        ),
        resolver,
        sources,
    )
    _parse_ok_union(result.sql)
    assert result.sql.upper().count("DISTINCT ON") == 3
