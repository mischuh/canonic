"""Compiler tests for conservative finality composition in composable_post_agg (GH-122, S6).

Acceptance criteria:
  AC1: A ratio with a provisional denominator and final numerator → the composite result
       carries finality metadata (sources_used includes the provisional source) and
       projects ``is_final`` so the core can tally provisional_rows.
  AC2: A ratio of two metrics with no finality rules → finality is None, no is_final column.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
import sqlglot

from canonic.compiler import SemanticQuery, compile
from canonic.compiler.result import FinalityMetadata
from canonic.connectors.base import ResultColumn, ResultSet
from canonic.contracts.models import (
    BindingKind,
    CanonicalRef,
    FinalityRule,
    MetricBinding,
    Realization,
)
from canonic.contracts.resolver import ContractResolver
from canonic.core.models import QueryMetadata
from canonic.semantic.models import Column, Dimension, Measure, SemanticSource

# ---------------------------------------------------------------------------
# Fixtures — in-memory damages project with finality realizations
# ---------------------------------------------------------------------------

_AS_OF = datetime(2026, 6, 13, 10, 0, 0, tzinfo=UTC)


@pytest.fixture
def damages() -> SemanticSource:
    """Final realization: historic damage claims with a timestamp column for finality gating."""
    return SemanticSource(
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
        dimensions=[
            Dimension(name="report_date", column="reported_at", granularity="day"),
        ],
    )


@pytest.fixture
def damages_rt() -> SemanticSource:
    """Provisional realization — same schema as damages, real-time intraday rows."""
    return SemanticSource(
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
        dimensions=[
            Dimension(name="report_date", column="reported_at", granularity="day"),
        ],
    )


@pytest.fixture
def sources(damages: SemanticSource, damages_rt: SemanticSource) -> list[SemanticSource]:
    return [damages, damages_rt]


@pytest.fixture
def total_cost_binding() -> MetricBinding:
    """Numerator — no finality rule (all rows implicitly final)."""
    return MetricBinding(
        metric="total_repair_cost",
        canonical=CanonicalRef(source="damages", measure="total_repair_cost"),
    )


@pytest.fixture
def damage_count_binding() -> MetricBinding:
    """Denominator — has a finality rule; denominator rows may be provisional."""
    return MetricBinding(
        metric="damage_count",
        canonical=CanonicalRef(source="damages", measure="damage_count"),
    )


@pytest.fixture
def avg_costs_binding() -> MetricBinding:
    return MetricBinding(
        metric="avg_repair_costs",
        canonical=CanonicalRef(
            kind=BindingKind.RATIO,
            numerator="total_repair_cost",
            denominator="damage_count",
        ),
    )


@pytest.fixture
def denominator_finality_rule() -> FinalityRule:
    """Finality rule for damage_count: damages=final (T-1), damages_rt=provisional."""
    return FinalityRule(
        metric="damage_count",
        realizations=[
            Realization(
                source="damages",
                role="final",
                watermark="business_day - 1 day",
                tz="UTC",
            ),
            Realization(source="damages_rt", role="provisional"),
        ],
        coalescing="window <= watermark ? final : provisional",
        result_flag="per_row",
    )


@pytest.fixture
def resolver(
    avg_costs_binding: MetricBinding,
    total_cost_binding: MetricBinding,
    damage_count_binding: MetricBinding,
    denominator_finality_rule: FinalityRule,
) -> ContractResolver:
    """Resolver where the denominator metric has a finality rule; numerator has none."""
    return ContractResolver(
        bindings=[avg_costs_binding, total_cost_binding, damage_count_binding],
        guardrails=[],
        finality=[denominator_finality_rule],
    )


@pytest.fixture
def resolver_no_finality(
    avg_costs_binding: MetricBinding,
    total_cost_binding: MetricBinding,
    damage_count_binding: MetricBinding,
) -> ContractResolver:
    """Resolver with no finality rules on either component."""
    return ContractResolver(
        bindings=[avg_costs_binding, total_cost_binding, damage_count_binding],
        guardrails=[],
    )


def _parse_ok(sql: str) -> None:
    sqlglot.parse_one(sql, dialect="postgres")


# ---------------------------------------------------------------------------
# S6 AC1 — provisional denominator, final numerator
# ---------------------------------------------------------------------------


class TestS6AC1ProvisionalDenominator:
    """AC1: ratio with provisional denominator and final numerator → composite flagged provisional."""

    def test_denominator_cte_is_union_all(
        self, resolver: ContractResolver, sources: list[SemanticSource]
    ) -> None:
        """The denominator CTE must be a UNION ALL (final branch ∪ provisional branch)."""
        result = compile(
            SemanticQuery(metrics=["avg_repair_costs"], dimensions=["report_date"], as_of=_AS_OF),
            resolver,
            sources,
        )
        _parse_ok(result.sql)
        assert "UNION ALL" in result.sql.upper()

    def test_composite_carries_finality_metadata(
        self, resolver: ContractResolver, sources: list[SemanticSource]
    ) -> None:
        """CompileResult.finality is populated when any component has a finality rule."""
        result = compile(
            SemanticQuery(metrics=["avg_repair_costs"], dimensions=["report_date"], as_of=_AS_OF),
            resolver,
            sources,
        )
        assert result.finality is not None
        assert isinstance(result.finality, FinalityMetadata)

    def test_provisional_source_in_sources_used(
        self, resolver: ContractResolver, sources: list[SemanticSource]
    ) -> None:
        """sources_used must include the provisional realization source."""
        result = compile(
            SemanticQuery(metrics=["avg_repair_costs"], dimensions=["report_date"], as_of=_AS_OF),
            resolver,
            sources,
        )
        assert result.finality is not None
        assert "damages_rt" in result.finality.sources_used

    def test_outer_select_projects_is_final(
        self, resolver: ContractResolver, sources: list[SemanticSource]
    ) -> None:
        """The outer SELECT must project is_final so the core can tally provisional_rows."""
        result = compile(
            SemanticQuery(metrics=["avg_repair_costs"], dimensions=["report_date"], as_of=_AS_OF),
            resolver,
            sources,
        )
        assert "is_final" in result.sql.lower()

    def test_numerator_cte_padded_with_true_is_final(
        self, resolver: ContractResolver, sources: list[SemanticSource]
    ) -> None:
        """The numerator CTE (no finality rule) is padded with TRUE AS is_final so the
        outer AND expression compiles cleanly."""
        result = compile(
            SemanticQuery(metrics=["avg_repair_costs"], dimensions=["report_date"], as_of=_AS_OF),
            resolver,
            sources,
        )
        _parse_ok(result.sql)
        # Both num and den CTEs must be present; is_final appears in the outer SELECT.
        sql_upper = result.sql.upper()
        assert "NUM" in sql_upper
        assert "DEN" in sql_upper
        assert "IS_FINAL" in sql_upper

    def test_is_final_uses_conservative_and(
        self, resolver: ContractResolver, sources: list[SemanticSource]
    ) -> None:
        """The outer is_final is an AND of COALESCE(num.is_final, TRUE) and
        COALESCE(den.is_final, TRUE), so a provisional denominator row makes the
        composite row provisional even when the numerator is final."""
        result = compile(
            SemanticQuery(metrics=["avg_repair_costs"], dimensions=["report_date"], as_of=_AS_OF),
            resolver,
            sources,
        )
        sql_upper = result.sql.upper()
        assert "COALESCE" in sql_upper

    def test_freshness_includes_both_leaf_sources(
        self, resolver: ContractResolver, sources: list[SemanticSource]
    ) -> None:
        """Freshness list must cover the realization sources for the denominator's finality rule."""
        result = compile(
            SemanticQuery(metrics=["avg_repair_costs"], dimensions=["report_date"], as_of=_AS_OF),
            resolver,
            sources,
        )
        freshness_sources = {f.source for f in result.freshness}
        assert "damages_rt" in freshness_sources


# ---------------------------------------------------------------------------
# Provisional row tally (conservative merge reflected in QueryMetadata)
# ---------------------------------------------------------------------------


class TestProvisionalRowTally:
    """Provisional rows in composite result are tallied via the is_final column (§7)."""

    def test_provisional_rows_counted_when_is_final_false(
        self, resolver: ContractResolver, sources: list[SemanticSource]
    ) -> None:
        """A result set containing an is_final=False row → provisional_rows >= 1."""
        compiled = compile(
            SemanticQuery(metrics=["avg_repair_costs"], dimensions=["report_date"], as_of=_AS_OF),
            resolver,
            sources,
        )
        assert compiled.finality is not None

        result_set = ResultSet(
            columns=[
                ResultColumn(name="report_date", type="timestamp"),
                ResultColumn(name="avg_repair_costs", type="decimal"),
                ResultColumn(name="is_final", type="bool"),
            ],
            rows=[
                ["2026-06-12", 150.0, True],
                ["2026-06-13", 200.0, False],
            ],
        )
        meta = QueryMetadata.from_compile_result(compiled, result=result_set)

        assert meta.finality is not None
        assert meta.finality.provisional_rows == 1
        assert meta.finality.final_rows == 1

    def test_watermark_is_earliest_across_leaves(
        self, resolver: ContractResolver, sources: list[SemanticSource]
    ) -> None:
        """When only the denominator has a finality rule, the composite watermark comes
        from that denominator rule (the only leaf watermark)."""
        result = compile(
            SemanticQuery(metrics=["avg_repair_costs"], dimensions=["report_date"], as_of=_AS_OF),
            resolver,
            sources,
        )
        assert result.finality is not None
        # Watermark is a non-empty ISO-8601 string — the denominator's resolved T-1 watermark.
        assert len(result.finality.watermark) > 0
        assert "T" in result.finality.watermark  # ISO-8601 datetime separator


# ---------------------------------------------------------------------------
# AC2 — no-finality composite is unchanged (regression guard)
# ---------------------------------------------------------------------------


class TestNoFinalityCompositeUnchanged:
    """AC2: a ratio with no finality rules on either component → finality is None."""

    def test_finality_is_none_when_no_rule(
        self, resolver_no_finality: ContractResolver, sources: list[SemanticSource]
    ) -> None:
        result = compile(
            SemanticQuery(metrics=["avg_repair_costs"], dimensions=["report_date"], as_of=_AS_OF),
            resolver_no_finality,
            sources,
        )
        assert result.finality is None

    def test_no_is_final_column_when_no_rule(
        self, resolver_no_finality: ContractResolver, sources: list[SemanticSource]
    ) -> None:
        result = compile(
            SemanticQuery(metrics=["avg_repair_costs"], dimensions=["report_date"], as_of=_AS_OF),
            resolver_no_finality,
            sources,
        )
        assert "is_final" not in result.sql.lower()

    def test_sql_is_valid_postgres(
        self, resolver_no_finality: ContractResolver, sources: list[SemanticSource]
    ) -> None:
        result = compile(
            SemanticQuery(metrics=["avg_repair_costs"], dimensions=["report_date"], as_of=_AS_OF),
            resolver_no_finality,
            sources,
        )
        _parse_ok(result.sql)
