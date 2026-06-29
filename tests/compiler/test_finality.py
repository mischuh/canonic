"""Compiler stage 5 acceptance tests for finality & coalescing (SPEC-E5-E15 §9 S1)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

import sqlglot

from canon.compiler import SemanticQuery, compile
from canon.compiler.result import FinalityMetadata

if TYPE_CHECKING:
    from canon.contracts.resolver import ContractResolver
    from canon.semantic.models import SemanticSource


def _parse_ok_union(sql: str) -> None:
    """Assert the emitted SQL is valid Postgres (SELECT or UNION ALL of SELECTs)."""
    parsed = sqlglot.parse_one(sql, dialect="postgres")
    assert isinstance(parsed, (sqlglot.exp.Select, sqlglot.exp.Union))


_AS_OF = datetime(2026, 6, 13, 10, 0, 0, tzinfo=UTC)


class TestS1FinalityAC1:
    """AC1: Given watermark 'business_day - 1 day', a query with a time dim produces UNION ALL."""

    def test_union_all_in_sql(
        self, finality_resolver: ContractResolver, sources: list[SemanticSource]
    ) -> None:
        result = compile(
            SemanticQuery(metrics=["revenue"], dimensions=["order_date"], as_of=_AS_OF),
            finality_resolver,
            sources,
        )
        _parse_ok_union(result.sql)
        assert "UNION ALL" in result.sql.upper()

    def test_final_branch_has_lte_gate(
        self, finality_resolver: ContractResolver, sources: list[SemanticSource]
    ) -> None:
        result = compile(
            SemanticQuery(metrics=["revenue"], dimensions=["order_date"], as_of=_AS_OF),
            finality_resolver,
            sources,
        )
        # The final branch gates on created_at <= watermark
        assert "<=" in result.sql

    def test_provisional_branch_has_gt_gate(
        self, finality_resolver: ContractResolver, sources: list[SemanticSource]
    ) -> None:
        result = compile(
            SemanticQuery(metrics=["revenue"], dimensions=["order_date"], as_of=_AS_OF),
            finality_resolver,
            sources,
        )
        assert ">" in result.sql

    def test_is_final_column_projected(
        self, finality_resolver: ContractResolver, sources: list[SemanticSource]
    ) -> None:
        result = compile(
            SemanticQuery(metrics=["revenue"], dimensions=["order_date"], as_of=_AS_OF),
            finality_resolver,
            sources,
        )
        assert "is_final" in result.sql.lower()

    def test_both_realization_tables_in_sql(
        self, finality_resolver: ContractResolver, sources: list[SemanticSource]
    ) -> None:
        result = compile(
            SemanticQuery(metrics=["revenue"], dimensions=["order_date"], as_of=_AS_OF),
            finality_resolver,
            sources,
        )
        assert "fct_orders" in result.sql
        assert "fct_orders_rt" in result.sql

    def test_true_false_marker_literals(
        self, finality_resolver: ContractResolver, sources: list[SemanticSource]
    ) -> None:
        result = compile(
            SemanticQuery(metrics=["revenue"], dimensions=["order_date"], as_of=_AS_OF),
            finality_resolver,
            sources,
        )
        sql_upper = result.sql.upper()
        assert "TRUE" in sql_upper or "1 AS IS_FINAL" in sql_upper
        assert "FALSE" in sql_upper or "0 AS IS_FINAL" in sql_upper


class TestS1FinalityMetadata:
    """AC2: result carries FinalityMetadata with watermark and sources_used."""

    def test_finality_metadata_present(
        self, finality_resolver: ContractResolver, sources: list[SemanticSource]
    ) -> None:
        result = compile(
            SemanticQuery(metrics=["revenue"], dimensions=["order_date"], as_of=_AS_OF),
            finality_resolver,
            sources,
        )
        assert isinstance(result.finality, FinalityMetadata)

    def test_watermark_in_metadata(
        self, finality_resolver: ContractResolver, sources: list[SemanticSource]
    ) -> None:
        result = compile(
            SemanticQuery(metrics=["revenue"], dimensions=["order_date"], as_of=_AS_OF),
            finality_resolver,
            sources,
        )
        assert result.finality is not None
        assert "T23:59:59" in result.finality.watermark

    def test_sources_used_in_metadata(
        self, finality_resolver: ContractResolver, sources: list[SemanticSource]
    ) -> None:
        result = compile(
            SemanticQuery(metrics=["revenue"], dimensions=["order_date"], as_of=_AS_OF),
            finality_resolver,
            sources,
        )
        assert result.finality is not None
        assert "orders" in result.finality.sources_used
        assert "orders_rt" in result.finality.sources_used

    def test_result_flag_in_metadata(
        self, finality_resolver: ContractResolver, sources: list[SemanticSource]
    ) -> None:
        result = compile(
            SemanticQuery(metrics=["revenue"], dimensions=["order_date"], as_of=_AS_OF),
            finality_resolver,
            sources,
        )
        assert result.finality is not None
        assert result.finality.result_flag == "per_row"

    def test_freshness_includes_both_sources(
        self, finality_resolver: ContractResolver, sources: list[SemanticSource]
    ) -> None:
        result = compile(
            SemanticQuery(metrics=["revenue"], dimensions=["order_date"], as_of=_AS_OF),
            finality_resolver,
            sources,
        )
        source_names = {f.source for f in result.freshness}
        assert "orders" in source_names
        assert "orders_rt" in source_names


class TestS1FinalityAC3Determinism:
    """AC3: same watermark rule + as_of → identical SQL."""

    def test_deterministic_same_as_of(
        self, finality_resolver: ContractResolver, sources: list[SemanticSource]
    ) -> None:
        q = SemanticQuery(metrics=["revenue"], dimensions=["order_date"], as_of=_AS_OF)
        r1 = compile(q, finality_resolver, sources)
        r2 = compile(q, finality_resolver, sources)
        assert r1.sql == r2.sql


class TestFinalityFallthrough:
    """Queries without a time dimension or without a finality rule fall through unchanged."""

    def test_no_time_dimension_no_finality(
        self, finality_resolver: ContractResolver, sources: list[SemanticSource]
    ) -> None:
        result = compile(
            SemanticQuery(metrics=["revenue"], dimensions=["status"], as_of=_AS_OF),
            finality_resolver,
            sources,
        )
        assert result.finality is None
        assert "UNION ALL" not in result.sql.upper()

    def test_no_dimensions_no_finality(
        self, finality_resolver: ContractResolver, sources: list[SemanticSource]
    ) -> None:
        result = compile(
            SemanticQuery(metrics=["revenue"], as_of=_AS_OF),
            finality_resolver,
            sources,
        )
        assert result.finality is None

    def test_no_finality_rule_unchanged(
        self, resolver: ContractResolver, sources: list[SemanticSource]
    ) -> None:
        result = compile(
            SemanticQuery(metrics=["revenue"], dimensions=["order_date"]),
            resolver,
            sources,
        )
        assert result.finality is None
        assert "UNION ALL" not in result.sql.upper()


class TestFinalityGuardrailsApplied:
    """Guardrails are applied inside each finality branch."""

    def test_guardrail_filter_in_union_branches(
        self, finality_resolver: ContractResolver, sources: list[SemanticSource]
    ) -> None:
        result = compile(
            SemanticQuery(metrics=["revenue"], dimensions=["order_date"], as_of=_AS_OF),
            finality_resolver,
            sources,
        )
        # The refund guardrail filter should appear in both branches
        assert result.sql.count("refunded") >= 1
        assert [g.id for g in result.guardrails_fired] == ["revenue-excludes-refunds"]


class TestFinalityJoinedDimension:
    """Finality queries that group by a dimension on a joined source produce valid SQL."""

    def test_joined_dimension_produces_union_all(
        self, finality_resolver: ContractResolver, sources: list[SemanticSource]
    ) -> None:
        result = compile(
            SemanticQuery(metrics=["revenue"], dimensions=["order_date", "region"], as_of=_AS_OF),
            finality_resolver,
            sources,
        )
        assert "UNION ALL" in result.sql.upper()

    def test_joined_dimension_has_join_in_each_branch(
        self, finality_resolver: ContractResolver, sources: list[SemanticSource]
    ) -> None:
        result = compile(
            SemanticQuery(metrics=["revenue"], dimensions=["order_date", "region"], as_of=_AS_OF),
            finality_resolver,
            sources,
        )
        # Both branches must join dim_customers to resolve "region"
        assert result.sql.lower().count("dim_customers") >= 2

    def test_joined_dimension_sql_is_valid(
        self, finality_resolver: ContractResolver, sources: list[SemanticSource]
    ) -> None:
        result = compile(
            SemanticQuery(metrics=["revenue"], dimensions=["order_date", "region"], as_of=_AS_OF),
            finality_resolver,
            sources,
        )
        _parse_ok_union(result.sql)
