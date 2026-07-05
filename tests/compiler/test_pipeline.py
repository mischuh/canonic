"""Compiler pipeline acceptance tests, tracking SPEC-E5-E15 §9 user stories S1–S5."""

from __future__ import annotations

import pytest
import sqlglot

from canonic import exc
from canonic.compiler import SemanticQuery, compile
from canonic.contracts.models import CanonicalRef, MetricBinding
from canonic.contracts.resolver import ContractResolver
from canonic.semantic.models import Column, Dimension, Measure, SemanticSource


def _parse_ok(sql: str) -> None:
    """Assert the emitted SQL is valid Postgres and SELECT-only."""
    parsed = sqlglot.parse_one(sql, dialect="postgres")
    assert isinstance(parsed, sqlglot.exp.Select)


# --- S1: compile a simple metric --------------------------------------------


def test_s1_ac1_metric_by_day(resolver: ContractResolver, sources: list[SemanticSource]) -> None:
    result = compile(
        SemanticQuery(metrics=["revenue"], dimensions=["order_date"]), resolver, sources
    )
    _parse_ok(result.sql)
    assert result.dialect == "postgres"
    assert result.resolved == {"revenue": "orders.total_revenue"}
    assert "SUM(" in result.sql.upper()
    assert "DATE_TRUNC" in result.sql.upper()
    assert "GROUP BY" in result.sql.upper()
    assert 'FROM "analytics"."fct_orders"' in result.sql


def test_s1_metric_resolves_via_alias(
    resolver: ContractResolver, sources: list[SemanticSource]
) -> None:
    result = compile(SemanticQuery(metrics=["rev"]), resolver, sources)
    assert result.resolved == {"rev": "orders.total_revenue"}


def test_s1_ac2_unknown_metric_unresolved(
    resolver: ContractResolver, sources: list[SemanticSource]
) -> None:
    with pytest.raises(exc.Unresolved) as ei:
        compile(SemanticQuery(metrics=["mrr"]), resolver, sources)
    assert ei.value.code is exc.ErrorCode.UNRESOLVED


def test_s1_ac3_ambiguous_metric_lists_candidates(sources: list[SemanticSource]) -> None:
    ambiguous = ContractResolver(
        bindings=[
            MetricBinding(
                metric="revenue", canonical=CanonicalRef(source="orders", measure="total_revenue")
            ),
            MetricBinding(
                metric="revenue",
                canonical=CanonicalRef(source="customers", measure="total_revenue"),
            ),
        ],
        guardrails=[],
    )
    with pytest.raises(exc.Ambiguous) as ei:
        compile(SemanticQuery(metrics=["revenue"]), ambiguous, sources)
    assert ei.value.code is exc.ErrorCode.AMBIGUOUS
    assert len(ei.value.candidates) == 2


# --- S2: mandatory-filter guardrail -----------------------------------------


def test_s2_ac1_mandatory_filter_anded_and_listed(
    resolver: ContractResolver, sources: list[SemanticSource]
) -> None:
    result = compile(SemanticQuery(metrics=["revenue"]), resolver, sources)
    assert "<> 'refunded'" in result.sql
    assert [g.id for g in result.guardrails_fired] == ["revenue-excludes-refunds"]
    assert result.guardrails_fired[0].kind == "mandatory_filter"


def test_s2_ac2_guardrail_anded_alongside_user_filter(
    resolver: ContractResolver, sources: list[SemanticSource]
) -> None:
    result = compile(
        SemanticQuery(metrics=["revenue"], filters=["status = 'completed'"]), resolver, sources
    )
    assert "= 'completed'" in result.sql
    assert "<> 'refunded'" in result.sql
    assert " AND " in result.sql.upper()


# --- S3: fanout handling ----------------------------------------------------


def test_s3_ac1_additive_fanout_dedup(
    resolver: ContractResolver, sources: list[SemanticSource]
) -> None:
    result = compile(SemanticQuery(metrics=["revenue"], dimensions=["sku"]), resolver, sources)
    _parse_ok(result.sql)
    # The fanning join is wrapped in a DISTINCT ON (grain) subquery before aggregation.
    assert "DISTINCT ON" in result.sql.upper()
    assert '"orders"."order_id"' in result.sql
    assert result.sql.upper().count("SUM(") == 1


# --- E15-S1: safety floor — reject-if-corrupting (SPEC-fuller-E15 §5, §11 S1) ----------


@pytest.fixture
def inventory_source() -> SemanticSource:
    """Semi-additive inventory snapshot: additive over warehouse, NOT over snapshot_date."""
    return SemanticSource(
        name="inventory_snapshots",
        connection="warehouse_pg",
        table="analytics.inventory_snapshots",
        grain=["warehouse_id", "snapshot_date"],
        columns=[
            Column(name="warehouse_id", type="string", nullable=False),
            Column(name="snapshot_date", type="date", nullable=False),
            Column(name="inventory_level", type="decimal", nullable=False),
        ],
        measures=[
            Measure(
                name="ending_inventory",
                expr="sum(inventory_level)",
                additivity="semi_additive",
                semi_additive_over=["snapshot_date"],
            )
        ],
        dimensions=[
            Dimension(name="warehouse_id", column="warehouse_id"),
            Dimension(name="snapshot_date", column="snapshot_date", granularity="day"),
        ],
    )


@pytest.fixture
def inventory_resolver(inventory_source: SemanticSource) -> ContractResolver:
    binding = MetricBinding(
        metric="ending_inventory",
        canonical=CanonicalRef(source="inventory_snapshots", measure="ending_inventory"),
    )
    return ContractResolver(bindings=[binding], guardrails=[])


def test_e15s1_ac2_non_additive_no_fanout_compiles(
    resolver: ContractResolver, sources: list[SemanticSource]
) -> None:
    """Non-additive at native grain with no fanout → compiles; base-row recompute is safe."""
    result = compile(SemanticQuery(metrics=["distinct_order_count"]), resolver, sources)
    _parse_ok(result.sql)
    assert "COUNT(DISTINCT" in result.sql.upper()
    assert "DISTINCT ON" not in result.sql.upper()


def test_e15s1_ac2_non_additive_many_to_one_join_compiles(
    resolver: ContractResolver, sources: list[SemanticSource]
) -> None:
    """Non-additive grouped via a many_to_one join (no row multiplication) → compiles."""
    result = compile(
        SemanticQuery(metrics=["distinct_order_count"], dimensions=["region"]), resolver, sources
    )
    _parse_ok(result.sql)
    assert "COUNT(DISTINCT" in result.sql.upper()
    assert "DISTINCT ON" not in result.sql.upper()


def test_e15s1_ac1_non_additive_fanout_raises_fanout_unsafe(
    resolver: ContractResolver, sources: list[SemanticSource]
) -> None:
    """Non-additive + one_to_many join → FanoutUnsafe with a rationale; never a wrong number."""
    with pytest.raises(exc.FanoutUnsafe) as ei:
        compile(
            SemanticQuery(metrics=["distinct_order_count"], dimensions=["sku"]), resolver, sources
        )
    assert ei.value.code is exc.ErrorCode.FANOUT_UNSAFE
    assert str(ei.value)


def test_e15s1_ac2_semi_additive_grouped_by_collapse_dim_compiles(
    inventory_resolver: ContractResolver, inventory_source: SemanticSource
) -> None:
    """Semi-additive grouped by its collapse dimension → additive over remaining dims; compiles."""
    result = compile(
        SemanticQuery(metrics=["ending_inventory"], dimensions=["snapshot_date"]),
        inventory_resolver,
        [inventory_source],
    )
    _parse_ok(result.sql)
    assert "SUM(" in result.sql.upper()


def test_e15s1_ac1_semi_additive_collapsed_raises_unsupported_measure(
    inventory_resolver: ContractResolver, inventory_source: SemanticSource
) -> None:
    """Semi-additive collapsed across its non-additive dimension → UnsupportedMeasure."""
    with pytest.raises(exc.UnsupportedMeasure) as ei:
        compile(
            SemanticQuery(metrics=["ending_inventory"], dimensions=["warehouse_id"]),
            inventory_resolver,
            [inventory_source],
        )
    assert ei.value.code is exc.ErrorCode.UNSUPPORTED_MEASURE
    assert str(ei.value)


# --- S4: no implicit or ambiguous joins -------------------------------------


def test_s4_ac1_unreachable_dimension_no_cross_join(
    resolver: ContractResolver, sources: list[SemanticSource]
) -> None:
    with pytest.raises(exc.Unreachable) as ei:
        compile(
            SemanticQuery(metrics=["revenue"], dimensions=["nonexistent_dim"]), resolver, sources
        )
    assert ei.value.code is exc.ErrorCode.UNREACHABLE


def test_s4_reachable_dimension_emits_join(
    resolver: ContractResolver, sources: list[SemanticSource]
) -> None:
    result = compile(SemanticQuery(metrics=["revenue"], dimensions=["region"]), resolver, sources)
    _parse_ok(result.sql)
    assert "JOIN" in result.sql.upper()
    assert '"analytics"."dim_customers"' in result.sql
    assert "CROSS JOIN" not in result.sql.upper()


def test_s4_join_on_clause_with_physical_table_name(
    resolver: ContractResolver, customers: SemanticSource
) -> None:
    """ON clause written with the physical table name must compile; regression for fct_orders alias bug."""
    from canonic.semantic.models import Join, Relationship

    orders = SemanticSource(
        name="orders",
        connection="warehouse_pg",
        table="analytics.fct_orders",
        grain=["order_id"],
        columns=[
            Column(name="order_id", type="string", nullable=False),
            Column(name="customer_id", type="string", nullable=False),
            Column(name="status", type="string", nullable=False),
            Column(name="amount", type="decimal", nullable=False),
            Column(name="created_at", type="timestamp", nullable=False),
        ],
        measures=[Measure(name="total_revenue", expr="sum(amount)", additivity="additive")],
        dimensions=[
            Dimension(name="order_date", column="created_at", granularity="day"),
            Dimension(name="status", column="status"),
        ],
        joins=[
            Join(
                to="customers",
                on="fct_orders.customer_id = customers.customer_id",
                relationship=Relationship.MANY_TO_ONE,
            ),
        ],
    )
    result = compile(
        SemanticQuery(metrics=["revenue"], dimensions=["region"]),
        resolver,
        [orders, customers],
    )
    _parse_ok(result.sql)
    assert "JOIN" in result.sql.upper()
    assert (
        '"fct_orders"."customer_id"' not in result.sql
    )  # ON clause must use alias, not table name


def test_s4_ac2_ambiguous_join_path(sources: list[SemanticSource]) -> None:
    from canonic.semantic.models import (
        Column,
        Dimension,
        Join,
        Measure,
        Relationship,
        SemanticSource,
    )

    o = SemanticSource(
        name="o",
        connection="c",
        table="t.o",
        grain=["id"],
        columns=[Column(name="id", type="string"), Column(name="amount", type="decimal")],
        measures=[Measure(name="m", expr="sum(amount)")],
        joins=[
            Join(to="a", on="o.id = a.id", relationship=Relationship.MANY_TO_ONE),
            Join(to="b", on="o.id = b.id", relationship=Relationship.MANY_TO_ONE),
        ],
    )
    a = SemanticSource(
        name="a",
        connection="c",
        table="t.a",
        grain=["id"],
        columns=[Column(name="id", type="string")],
        joins=[Join(to="c", on="a.id = c.id", relationship=Relationship.MANY_TO_ONE)],
    )
    b = SemanticSource(
        name="b",
        connection="c",
        table="t.b",
        grain=["id"],
        columns=[Column(name="id", type="string")],
        joins=[Join(to="c", on="b.id = c.id", relationship=Relationship.MANY_TO_ONE)],
    )
    c = SemanticSource(
        name="c",
        connection="c",
        table="t.c",
        grain=["id"],
        columns=[Column(name="id", type="string"), Column(name="region", type="string")],
        dimensions=[Dimension(name="c_region", column="region")],
    )
    res = ContractResolver(
        bindings=[MetricBinding(metric="m", canonical=CanonicalRef(source="o", measure="m"))],
        guardrails=[],
    )
    with pytest.raises(exc.AmbiguousJoinPath) as ei:
        compile(SemanticQuery(metrics=["m"], dimensions=["c_region"]), res, [o, a, b, c])
    assert ei.value.code is exc.ErrorCode.AMBIGUOUS_JOIN_PATH
    assert len(ei.value.candidates) == 2
    assert ei.value.owner == "o"
    assert ei.value.target == "c"
    from canonic.compiler.joins import JoinPathCandidate

    vias = {tuple(c.via) for c in ei.value.candidates}
    assert vias == {("a", "c"), ("b", "c")}
    for c in ei.value.candidates:
        assert isinstance(c, JoinPathCandidate)
        assert c.route.startswith("o →")
        assert len(c.joins) == len(c.via)


def test_s4_ac2_via_resolves_ambiguity(sources: list[SemanticSource]) -> None:
    from canonic.semantic.models import (
        Column,
        Dimension,
        Join,
        Measure,
        Relationship,
        SemanticSource,
    )

    o = SemanticSource(
        name="o",
        connection="c",
        table="t.o",
        grain=["id"],
        columns=[Column(name="id", type="string"), Column(name="amount", type="decimal")],
        measures=[Measure(name="m", expr="sum(amount)")],
        joins=[
            Join(to="a", on="o.id = a.id", relationship=Relationship.MANY_TO_ONE),
            Join(to="b", on="o.id = b.id", relationship=Relationship.MANY_TO_ONE),
        ],
    )
    a = SemanticSource(
        name="a",
        connection="c",
        table="t.a",
        grain=["id"],
        columns=[Column(name="id", type="string")],
        joins=[Join(to="c", on="a.id = c.id", relationship=Relationship.MANY_TO_ONE)],
    )
    b = SemanticSource(
        name="b",
        connection="c",
        table="t.b",
        grain=["id"],
        columns=[Column(name="id", type="string")],
        joins=[Join(to="c", on="b.id = c.id", relationship=Relationship.MANY_TO_ONE)],
    )
    c = SemanticSource(
        name="c",
        connection="c",
        table="t.c",
        grain=["id"],
        columns=[Column(name="id", type="string"), Column(name="region", type="string")],
        dimensions=[Dimension(name="c_region", column="region")],
    )
    res = ContractResolver(
        bindings=[MetricBinding(metric="m", canonical=CanonicalRef(source="o", measure="m"))],
        guardrails=[],
    )
    result = compile(
        SemanticQuery(metrics=["m"], dimensions=["c_region"], via=["a"]), res, [o, a, b, c]
    )
    _parse_ok(result.sql)
    assert '"t"."a"' in result.sql
    assert '"t"."b"' not in result.sql


def test_s4_ac2_via_no_matching_path_raises_unreachable(sources: list[SemanticSource]) -> None:
    from canonic.semantic.models import (
        Column,
        Dimension,
        Join,
        Measure,
        Relationship,
        SemanticSource,
    )

    o = SemanticSource(
        name="o",
        connection="c",
        table="t.o",
        grain=["id"],
        columns=[Column(name="id", type="string"), Column(name="amount", type="decimal")],
        measures=[Measure(name="m", expr="sum(amount)")],
        joins=[
            Join(to="a", on="o.id = a.id", relationship=Relationship.MANY_TO_ONE),
            Join(to="b", on="o.id = b.id", relationship=Relationship.MANY_TO_ONE),
        ],
    )
    a = SemanticSource(
        name="a",
        connection="c",
        table="t.a",
        grain=["id"],
        columns=[Column(name="id", type="string")],
        joins=[Join(to="c", on="a.id = c.id", relationship=Relationship.MANY_TO_ONE)],
    )
    b = SemanticSource(
        name="b",
        connection="c",
        table="t.b",
        grain=["id"],
        columns=[Column(name="id", type="string")],
        joins=[Join(to="c", on="b.id = c.id", relationship=Relationship.MANY_TO_ONE)],
    )
    c = SemanticSource(
        name="c",
        connection="c",
        table="t.c",
        grain=["id"],
        columns=[Column(name="id", type="string"), Column(name="region", type="string")],
        dimensions=[Dimension(name="c_region", column="region")],
    )
    res = ContractResolver(
        bindings=[MetricBinding(metric="m", canonical=CanonicalRef(source="o", measure="m"))],
        guardrails=[],
    )
    with pytest.raises(exc.Unreachable):
        compile(SemanticQuery(metrics=["m"], dimensions=["c_region"], via=["z"]), res, [o, a, b, c])


# --- S5: deterministic output -----------------------------------------------


@pytest.mark.release_gate
def test_s5_byte_identical_on_repeat(
    resolver: ContractResolver, sources: list[SemanticSource]
) -> None:
    query = SemanticQuery(
        metrics=["revenue"], dimensions=["order_date", "status"], filters=["status = 'completed'"]
    )
    first = compile(query, resolver, sources)
    second = compile(query, resolver, sources)
    assert first.sql == second.sql
    assert [g.id for g in first.guardrails_fired] == [g.id for g in second.guardrails_fired]


def test_freshness_reports_used_sources(
    resolver: ContractResolver, sources: list[SemanticSource]
) -> None:
    result = compile(SemanticQuery(metrics=["revenue"], dimensions=["region"]), resolver, sources)
    reported = [f.source for f in result.freshness]
    assert reported == sorted(reported)
    assert "orders" in reported
    assert "customers" in reported


# --- dimension resolution: owner-aware priority -----------------------------


def test_owner_wins_over_alphabetically_earlier_source(
    resolver: ContractResolver,
    orders: SemanticSource,
    accounts: SemanticSource,
) -> None:
    """Owner 'orders' has 'status'; 'accounts' also has 'status' but is alphabetically first.
    The compiler must bind status to orders.status — no join to accounts."""
    result = compile(
        SemanticQuery(metrics=["revenue"], dimensions=["status"]),
        resolver,
        [orders, accounts],
    )
    _parse_ok(result.sql)
    assert "accounts" not in result.sql
    assert "JOIN" not in result.sql.upper()
    assert "orders" in result.sql


def test_unlinked_source_with_same_dim_does_not_block_query(
    resolver: ContractResolver,
    orders: SemanticSource,
    accounts: SemanticSource,
) -> None:
    """Regression: previously raised Unreachable because 'accounts' was selected over 'orders'."""
    result = compile(
        SemanticQuery(metrics=["revenue"], dimensions=["status"]),
        resolver,
        [orders, accounts],
    )
    assert result.resolved == {"revenue": "orders.total_revenue"}


def test_ambiguous_dim_across_two_reachable_sources_raises(
    resolver: ContractResolver, orders: SemanticSource
) -> None:
    """Two join-reachable sources both declaring 'tier' → Ambiguous, not a silent pick."""
    from canonic.semantic.models import Column, Dimension, Join, Relationship, SemanticSource

    left = SemanticSource(
        name="left_src",
        connection="c",
        table="t.left",
        grain=["id"],
        columns=[Column(name="id", type="string"), Column(name="tier", type="string")],
        dimensions=[Dimension(name="tier", column="tier")],
    )
    right = SemanticSource(
        name="right_src",
        connection="c",
        table="t.right",
        grain=["id"],
        columns=[Column(name="id", type="string"), Column(name="tier", type="string")],
        dimensions=[Dimension(name="tier", column="tier")],
    )
    orders_with_joins = SemanticSource(
        name="orders",
        connection=orders.connection,
        table=orders.table,
        grain=orders.grain,
        columns=orders.columns,
        measures=orders.measures,
        dimensions=orders.dimensions,
        joins=[
            Join(
                to="left_src",
                on="orders.order_id = left_src.id",
                relationship=Relationship.MANY_TO_ONE,
            ),
            Join(
                to="right_src",
                on="orders.order_id = right_src.id",
                relationship=Relationship.MANY_TO_ONE,
            ),
        ],
    )
    with pytest.raises(exc.Ambiguous) as ei:
        compile(
            SemanticQuery(metrics=["revenue"], dimensions=["tier"]),
            resolver,
            [orders_with_joins, left, right],
        )
    assert ei.value.code is exc.ErrorCode.AMBIGUOUS
    assert set(ei.value.candidates) == {"left_src.tier", "right_src.tier"}


# --- Named joins: role-qualified dimensions ----------------------------------


def _make_role_sources() -> tuple[object, object, object]:
    """Build an owner with two named joins to the same 'loc' source (pickup/dropoff)."""
    from canonic.semantic.models import (
        Column,
        Dimension,
        Join,
        Measure,
        Relationship,
        SemanticSource,
    )

    loc = SemanticSource(
        name="loc",
        connection="c",
        table="t.loc",
        grain=["id"],
        columns=[Column(name="id", type="string"), Column(name="city", type="string")],
        dimensions=[Dimension(name="city", column="city")],
    )
    owner = SemanticSource(
        name="owner",
        connection="c",
        table="t.owner",
        grain=["id"],
        columns=[Column(name="id", type="string"), Column(name="amount", type="decimal")],
        measures=[Measure(name="m", expr="sum(amount)")],
        joins=[
            Join(
                name="pickup",
                to="loc",
                on="owner.id = loc.id",
                relationship=Relationship.MANY_TO_ONE,
            ),
            Join(
                name="dropoff",
                to="loc",
                on="owner.id = loc.id",
                relationship=Relationship.MANY_TO_ONE,
            ),
        ],
    )
    res = ContractResolver(
        bindings=[MetricBinding(metric="m", canonical=CanonicalRef(source="owner", measure="m"))],
        guardrails=[],
    )
    return owner, loc, res


def test_named_join_qualified_dim_compiles() -> None:
    owner, loc, res = _make_role_sources()
    result = compile(SemanticQuery(metrics=["m"], dimensions=["pickup.city"]), res, [owner, loc])
    _parse_ok(result.sql)
    assert "pickup" in result.sql
    assert "dropoff" not in result.sql
    assert '"t"."loc"' in result.sql or "t.loc" in result.sql


def test_named_join_both_roles_compile() -> None:
    owner, loc, res = _make_role_sources()
    result = compile(
        SemanticQuery(metrics=["m"], dimensions=["pickup.city", "dropoff.city"]),
        res,
        [owner, loc],
    )
    _parse_ok(result.sql)
    assert "pickup" in result.sql
    assert "dropoff" in result.sql


def test_named_join_unqualified_ambiguous_dim_raises() -> None:
    owner, loc, res = _make_role_sources()
    with pytest.raises(exc.Ambiguous) as ei:
        compile(SemanticQuery(metrics=["m"], dimensions=["city"]), res, [owner, loc])
    assert ei.value.code is exc.ErrorCode.AMBIGUOUS
    assert set(ei.value.candidates) == {"pickup.city", "dropoff.city"}


def test_named_join_unknown_role_raises_unreachable() -> None:
    owner, loc, res = _make_role_sources()
    with pytest.raises(exc.Unreachable):
        compile(SemanticQuery(metrics=["m"], dimensions=["warehouse.city"]), res, [owner, loc])


def test_named_join_duplicate_alias_raises_validation_error() -> None:
    from pydantic import ValidationError

    from canonic.semantic.models import Column, Join, Relationship, SemanticSource

    with pytest.raises(ValidationError, match="duplicate join alias"):
        SemanticSource(
            name="bad",
            connection="c",
            table="t.bad",
            grain=["id"],
            columns=[Column(name="id", type="string")],
            joins=[
                Join(to="loc", on="bad.id = loc.id", relationship=Relationship.MANY_TO_ONE),
                Join(to="loc", on="bad.id = loc.id", relationship=Relationship.MANY_TO_ONE),
            ],
        )


# --- SQLite-style DATE() filter rewriting ------------------------------------


def test_filter_sqlite_date_subtraction(
    resolver: ContractResolver, sources: list[SemanticSource]
) -> None:
    result = compile(
        SemanticQuery(
            metrics=["revenue"],
            filters=["created_at >= DATE('now', '-3 months')"],
        ),
        resolver,
        sources,
    )
    _parse_ok(result.sql)
    assert "CURRENT_DATE" in result.sql.upper()
    assert "INTERVAL" in result.sql.upper()


def test_filter_sqlite_date_addition(
    resolver: ContractResolver, sources: list[SemanticSource]
) -> None:
    result = compile(
        SemanticQuery(
            metrics=["revenue"],
            filters=["created_at <= DATE('now', '+1 months')"],
        ),
        resolver,
        sources,
    )
    _parse_ok(result.sql)
    assert "CURRENT_DATE" in result.sql.upper()
    assert "INTERVAL" in result.sql.upper()


# --- Dialect-aware emission ---------------------------------------------------


def test_compile_uses_postgres_dialect_by_default(
    resolver: ContractResolver, sources: list[SemanticSource]
) -> None:
    result = compile(SemanticQuery(metrics=["revenue"]), resolver, sources)
    assert result.dialect == "postgres"


def test_compile_duckdb_dialect_via_connection_map(
    resolver: ContractResolver, sources: list[SemanticSource]
) -> None:
    result = compile(
        SemanticQuery(metrics=["revenue"]),
        resolver,
        sources,
        connection_dialects={"warehouse_pg": "duckdb"},
    )
    assert result.dialect == "duckdb"


def test_compile_duckdb_date_filter_emits_duckdb_interval(
    resolver: ContractResolver, sources: list[SemanticSource]
) -> None:
    result = compile(
        SemanticQuery(
            metrics=["revenue"],
            filters=["created_at >= DATE('now', '-3 months')"],
        ),
        resolver,
        sources,
        connection_dialects={"warehouse_pg": "duckdb"},
    )
    assert result.dialect == "duckdb"
    assert "CURRENT_DATE" in result.sql.upper()
    # DuckDB uses INTERVAL '3' MONTHS (separate tokens)
    assert "INTERVAL '3' MONTHS" in result.sql


def test_compile_postgres_date_filter_emits_postgres_interval(
    resolver: ContractResolver, sources: list[SemanticSource]
) -> None:
    result = compile(
        SemanticQuery(
            metrics=["revenue"],
            filters=["created_at >= DATE('now', '-3 months')"],
        ),
        resolver,
        sources,
    )
    assert result.dialect == "postgres"
    assert "CURRENT_DATE" in result.sql.upper()
    # Postgres uses INTERVAL '3 MONTHS' (unit inside the string)
    assert "INTERVAL '3 MONTHS'" in result.sql


def test_compile_empty_connection_map_falls_back_to_postgres(
    resolver: ContractResolver, sources: list[SemanticSource]
) -> None:
    result = compile(
        SemanticQuery(metrics=["revenue"]),
        resolver,
        sources,
        connection_dialects={},
    )
    assert result.dialect == "postgres"


def test_compile_unknown_connection_falls_back_to_postgres(
    resolver: ContractResolver, sources: list[SemanticSource]
) -> None:
    result = compile(
        SemanticQuery(metrics=["revenue"]),
        resolver,
        sources,
        connection_dialects={"other_connection": "duckdb"},
    )
    assert result.dialect == "postgres"
