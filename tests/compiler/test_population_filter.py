"""Compiler tests for population_filter — one population-restriction mechanism across all kinds (GH-128, S2b).

Acceptance criteria covered here:
  AC1: avg_repair_costs with population_filter: "severity IN ('major','moderate')" queried by
       severity → both numerator and denominator subqueries exclude minor-severity rows before
       aggregating.
  AC2: The same restriction expressed as a mandatory_filter guardrail with
       applies_to: { metric: avg_repair_costs } does NOT fire on either leaf (regression guard
       against the silent no-op failure mode).

Cross-ticket ACs (not covered here):
  AC3 — column absent from a leaf's source → VALIDATION_FAILED at write time (covered in #123).
  AC4 — distinct_count population_filter (covered in #120 / test_recompute_at_grain.py).
"""

from __future__ import annotations

import pytest
import sqlglot

from canon.compiler import SemanticQuery, compile
from canon.contracts.models import (
    AppliesTo,
    BindingKind,
    CanonicalRef,
    CollapseAgg,
    Guardrail,
    GuardrailKind,
    MetricBinding,
    Severity,
)
from canon.contracts.resolver import ContractResolver
from canon.semantic.models import Column, Dimension, Measure, SemanticSource

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _parse_ok(sql: str) -> None:
    sqlglot.parse_one(sql, dialect="postgres")


# ---------------------------------------------------------------------------
# Fixtures — damages source with severity column (ratio / single tests)
# ---------------------------------------------------------------------------


@pytest.fixture
def damages() -> SemanticSource:
    """Fact table with a severity column so population_filter can reference it."""
    return SemanticSource(
        name="damages",
        connection="warehouse_pg",
        table="fct_damages",
        grain=["damage_id"],
        columns=[
            Column(name="damage_id", type="string", nullable=False),
            Column(name="repair_cost", type="decimal", nullable=True),
            Column(name="severity", type="string", nullable=False),
            Column(name="reported_month", type="string", nullable=False),
        ],
        measures=[
            Measure(name="total_repair_cost", expr="sum(repair_cost)", additivity="additive"),
            Measure(name="damage_count", expr="count(damage_id)", additivity="additive"),
        ],
        dimensions=[
            Dimension(name="reported_month", column="reported_month"),
            Dimension(name="severity", column="severity"),
        ],
    )


@pytest.fixture
def total_cost_binding() -> MetricBinding:
    return MetricBinding(
        metric="total_repair_cost",
        canonical=CanonicalRef(source="damages", measure="total_repair_cost"),
    )


@pytest.fixture
def damage_count_binding() -> MetricBinding:
    return MetricBinding(
        metric="damage_count",
        canonical=CanonicalRef(source="damages", measure="damage_count"),
    )


@pytest.fixture
def avg_costs_with_filter(
    total_cost_binding: MetricBinding, damage_count_binding: MetricBinding
) -> MetricBinding:
    return MetricBinding(
        metric="avg_repair_costs",
        canonical=CanonicalRef(
            kind=BindingKind.RATIO,
            numerator="total_repair_cost",
            denominator="damage_count",
            population_filter="severity IN ('major', 'moderate')",
        ),
    )


@pytest.fixture
def resolver_ratio(
    avg_costs_with_filter: MetricBinding,
    total_cost_binding: MetricBinding,
    damage_count_binding: MetricBinding,
) -> ContractResolver:
    return ContractResolver(
        bindings=[avg_costs_with_filter, total_cost_binding, damage_count_binding],
        guardrails=[],
    )


# ---------------------------------------------------------------------------
# AC1 — ratio: population_filter appears in both numerator and denominator CTEs
# ---------------------------------------------------------------------------


def test_ac1_population_filter_in_both_ctes(
    resolver_ratio: ContractResolver, damages: SemanticSource
) -> None:
    """Both the num and den CTEs carry the severity filter before aggregating."""
    result = compile(SemanticQuery(metrics=["avg_repair_costs"]), resolver_ratio, [damages])
    _parse_ok(result.sql)
    sql_lower = result.sql.lower()
    # "severity" should appear at least twice — once per CTE WHERE clause.
    assert sql_lower.count("severity") >= 2
    assert "major" in sql_lower
    assert "moderate" in sql_lower
    # Structural sanity: ratio produces two CTEs + CROSS JOIN (scalar).
    assert "with" in sql_lower
    assert "cross join" in sql_lower


def test_ac1_population_filter_with_grouping(
    resolver_ratio: ContractResolver, damages: SemanticSource
) -> None:
    """Grouped by severity: filter still appears in both CTEs alongside GROUP BY."""
    result = compile(
        SemanticQuery(metrics=["avg_repair_costs"], dimensions=["severity"]),
        resolver_ratio,
        [damages],
    )
    _parse_ok(result.sql)
    sql_lower = result.sql.lower()
    assert sql_lower.count("severity") >= 2
    assert "full" in sql_lower  # FULL JOIN because there are dimensions


def test_ac1_population_filter_model_roundtrip() -> None:
    """population_filter survives model construction and is accessible on CanonicalRef."""
    ref = CanonicalRef(
        kind=BindingKind.RATIO,
        numerator="a",
        denominator="b",
        population_filter="severity IN ('major', 'moderate')",
    )
    assert ref.population_filter == "severity IN ('major', 'moderate')"


def test_ac1_no_filter_when_none(
    total_cost_binding: MetricBinding,
    damage_count_binding: MetricBinding,
    damages: SemanticSource,
) -> None:
    """Without population_filter the severity column is absent from the SQL."""
    binding = MetricBinding(
        metric="avg_repair_costs",
        canonical=CanonicalRef(
            kind=BindingKind.RATIO,
            numerator="total_repair_cost",
            denominator="damage_count",
        ),
    )
    resolver = ContractResolver(
        bindings=[binding, total_cost_binding, damage_count_binding], guardrails=[]
    )
    result = compile(SemanticQuery(metrics=["avg_repair_costs"]), resolver, [damages])
    assert "severity" not in result.sql.lower()


# ---------------------------------------------------------------------------
# AC2 — regression: metric-level mandatory_filter guardrail fires on neither leaf
# ---------------------------------------------------------------------------


def test_ac2_metric_level_guardrail_does_not_fire(
    avg_costs_with_filter: MetricBinding,
    total_cost_binding: MetricBinding,
    damage_count_binding: MetricBinding,
    damages: SemanticSource,
) -> None:
    """A mandatory_filter guardrail keyed to the composite metric name never fires.

    guardrails_for() is called per leaf on (source, measure); a ratio metric has no single
    (source, measure) and is excluded from _metric_to_canonical, so the match always fails.
    """
    metric_guardrail = Guardrail(
        id="avg-repair-costs-exclude-minor",
        applies_to=AppliesTo(metric="avg_repair_costs"),
        kind=GuardrailKind.MANDATORY_FILTER,
        filter="severity IN ('major', 'moderate')",
        severity=Severity.ERROR,
        rationale="This is the broken pattern — metric-level guardrail on a composite metric.",
    )
    resolver = ContractResolver(
        bindings=[avg_costs_with_filter, total_cost_binding, damage_count_binding],
        guardrails=[metric_guardrail],
    )
    result = compile(SemanticQuery(metrics=["avg_repair_costs"]), resolver, [damages])
    assert not any(g.id == "avg-repair-costs-exclude-minor" for g in result.guardrails_fired)


# ---------------------------------------------------------------------------
# semi_additive — population_filter injected into the inner CTE before collapse
# ---------------------------------------------------------------------------


@pytest.fixture
def inventory_source() -> SemanticSource:
    """Daily snapshot with a warehouse_status column for population_filter testing."""
    return SemanticSource(
        name="inventory_snapshots",
        connection="warehouse_pg",
        table="analytics.inventory_snapshots",
        grain=["warehouse_id", "snapshot_date"],
        columns=[
            Column(name="warehouse_id", type="string", nullable=False),
            Column(name="snapshot_date", type="date", nullable=False),
            Column(name="inventory_level", type="decimal", nullable=False),
            Column(name="warehouse_status", type="string", nullable=False),
        ],
        measures=[
            Measure(
                name="inventory_level",
                expr="sum(inventory_level)",
                additivity="additive",
            )
        ],
        dimensions=[
            Dimension(name="warehouse_id", column="warehouse_id"),
            Dimension(name="snapshot_date", column="snapshot_date", granularity="day"),
        ],
    )


@pytest.fixture
def ending_inventory_with_filter() -> MetricBinding:
    return MetricBinding(
        metric="ending_inventory",
        canonical=CanonicalRef(
            kind=BindingKind.SEMI_ADDITIVE,
            source="inventory_snapshots",
            measure="inventory_level",
            collapse_dimension="snapshot_date",
            collapse_agg=CollapseAgg.LAST,
            population_filter="warehouse_status = 'active'",
        ),
    )


@pytest.fixture
def resolver_sa(ending_inventory_with_filter: MetricBinding) -> ContractResolver:
    return ContractResolver(bindings=[ending_inventory_with_filter], guardrails=[])


def test_semi_additive_population_filter_collapsed(
    resolver_sa: ContractResolver, inventory_source: SemanticSource
) -> None:
    """Collapsed (no snapshot_date in dims): filter appears in the inner CTE before ROW_NUMBER."""
    result = compile(
        SemanticQuery(metrics=["ending_inventory"], dimensions=["warehouse_id"]),
        resolver_sa,
        [inventory_source],
    )
    _parse_ok(result.sql)
    sql_lower = result.sql.lower()
    assert "warehouse_status" in sql_lower
    assert "active" in sql_lower
    # Collapsed form uses ROW_NUMBER window.
    assert "row_number()" in sql_lower
    assert result.partial_additive is not None
    assert result.partial_additive.collapsed is True


def test_semi_additive_population_filter_additive_branch(
    resolver_sa: ContractResolver, inventory_source: SemanticSource
) -> None:
    """Additive branch (grouped by snapshot_date): filter still appears in WHERE."""
    result = compile(
        SemanticQuery(metrics=["ending_inventory"], dimensions=["snapshot_date"]),
        resolver_sa,
        [inventory_source],
    )
    _parse_ok(result.sql)
    sql_lower = result.sql.lower()
    assert "warehouse_status" in sql_lower
    assert "active" in sql_lower
    assert result.partial_additive is not None
    assert result.partial_additive.collapsed is False


# ---------------------------------------------------------------------------
# single kind — population_filter wired through the standard compile path
# ---------------------------------------------------------------------------


def test_single_population_filter_in_where(damages: SemanticSource) -> None:
    """Single-kind binding with population_filter: predicate lands in the WHERE clause."""
    binding = MetricBinding(
        metric="total_repair_cost",
        canonical=CanonicalRef(
            source="damages",
            measure="total_repair_cost",
            population_filter="severity IN ('major', 'moderate')",
        ),
    )
    resolver = ContractResolver(bindings=[binding], guardrails=[])
    result = compile(SemanticQuery(metrics=["total_repair_cost"]), resolver, [damages])
    _parse_ok(result.sql)
    sql_lower = result.sql.lower()
    assert "severity" in sql_lower
    assert "major" in sql_lower
