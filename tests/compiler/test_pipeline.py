"""Compiler pipeline acceptance tests, tracking SPEC-E5-E15 §9 user stories S1–S5."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
import sqlglot

from canon import exc
from canon.compiler import SemanticQuery, compile
from canon.contracts.models import CanonicalRef, MetricBinding
from canon.contracts.resolver import ContractResolver

if TYPE_CHECKING:
    from canon.semantic.models import SemanticSource


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


def test_s3_ac2_non_additive_measure_unsupported(
    resolver: ContractResolver, sources: list[SemanticSource]
) -> None:
    with pytest.raises(exc.UnsupportedMeasure) as ei:
        compile(SemanticQuery(metrics=["distinct_order_count"]), resolver, sources)
    assert ei.value.code is exc.ErrorCode.UNSUPPORTED_MEASURE


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


def test_s4_ac2_ambiguous_join_path(sources: list[SemanticSource]) -> None:
    from canon.semantic.models import Column, Dimension, Join, Measure, Relationship, SemanticSource

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


def test_s4_ac2_via_resolves_ambiguity(sources: list[SemanticSource]) -> None:
    from canon.semantic.models import Column, Dimension, Join, Measure, Relationship, SemanticSource

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
    from canon.semantic.models import Column, Dimension, Join, Measure, Relationship, SemanticSource

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
    from canon.semantic.models import Column, Dimension, Join, Relationship, SemanticSource

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
    assert set(ei.value.candidates) == {"left_src", "right_src"}
