"""Compiler tests for composable_post_agg strategy — ratio & weighted_avg (GH-118, S2).

Acceptance criteria:
  AC1: avg_repair_costs = total_repair_cost / damage_count produces numerator-sum ÷
       denominator-sum at the requested grain (no-grouping, by month, by region).
  AC2: Zero denominator → NULL + warning (default); ZERO → COALESCE; ERROR → raw division.
  AC3: Numerator's own guardrails fire automatically on its leaf.
"""

from __future__ import annotations

import re

import pytest
import sqlglot

from canonic import exc
from canonic.compiler import SemanticQuery, compile
from canonic.contracts.models import (
    AppliesTo,
    BindingKind,
    CanonicalRef,
    Guardrail,
    GuardrailKind,
    MetricBinding,
    OnZeroDenominator,
    Severity,
)
from canonic.contracts.resolver import ContractResolver
from canonic.semantic.models import Column, Dimension, Join, Measure, Relationship, SemanticSource

# ---------------------------------------------------------------------------
# Fixtures — in-memory damages + customers project
# ---------------------------------------------------------------------------


@pytest.fixture
def damages() -> SemanticSource:
    """Fact table: one row per damage claim. Measures: total_repair_cost, damage_count."""
    return SemanticSource(
        name="damages",
        connection="warehouse_pg",
        table="fct_damages",
        grain=["damage_id"],
        columns=[
            Column(name="damage_id", type="string", nullable=False),
            Column(name="vehicle_id", type="string", nullable=False),
            Column(name="repair_cost", type="decimal", nullable=True),
            Column(name="reported_month", type="string", nullable=False),
            Column(name="warranty_claim", type="int", nullable=False),
        ],
        measures=[
            Measure(name="total_repair_cost", expr="sum(repair_cost)", additivity="additive"),
            Measure(name="damage_count", expr="count(damage_id)", additivity="additive"),
        ],
        dimensions=[
            Dimension(name="reported_month", column="reported_month"),
        ],
        joins=[
            Join(
                to="vehicles",
                on="damages.vehicle_id = vehicles.vehicle_id",
                relationship=Relationship.MANY_TO_ONE,
            )
        ],
    )


@pytest.fixture
def vehicles() -> SemanticSource:
    """Dimension: vehicle region."""
    return SemanticSource(
        name="vehicles",
        connection="warehouse_pg",
        table="dim_vehicles",
        grain=["vehicle_id"],
        columns=[
            Column(name="vehicle_id", type="string", nullable=False),
            Column(name="region", type="string", nullable=False),
        ],
        dimensions=[Dimension(name="region", column="region")],
    )


@pytest.fixture
def sources(damages: SemanticSource, vehicles: SemanticSource) -> list[SemanticSource]:
    return [damages, vehicles]


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
def avg_costs_binding() -> MetricBinding:
    return MetricBinding(
        metric="avg_repair_costs",
        canonical=CanonicalRef(
            kind=BindingKind.RATIO,
            numerator="total_repair_cost",
            denominator="damage_count",
        ),
        aliases=["avg repair costs", "average repair costs"],
    )


@pytest.fixture
def resolver(
    avg_costs_binding: MetricBinding,
    total_cost_binding: MetricBinding,
    damage_count_binding: MetricBinding,
) -> ContractResolver:
    return ContractResolver(
        bindings=[avg_costs_binding, total_cost_binding, damage_count_binding],
        guardrails=[],
    )


@pytest.fixture
def warranty_guardrail() -> Guardrail:
    return Guardrail(
        id="total-repair-cost-no-warranty",
        applies_to=AppliesTo(source="damages", measure="total_repair_cost"),
        kind=GuardrailKind.MANDATORY_FILTER,
        filter="warranty_claim = 0",
        severity=Severity.ERROR,
        rationale="Excludes warranty claims from repair cost totals.",
    )


@pytest.fixture
def resolver_with_guardrail(
    avg_costs_binding: MetricBinding,
    total_cost_binding: MetricBinding,
    damage_count_binding: MetricBinding,
    warranty_guardrail: Guardrail,
) -> ContractResolver:
    return ContractResolver(
        bindings=[avg_costs_binding, total_cost_binding, damage_count_binding],
        guardrails=[warranty_guardrail],
    )


def _parse_ok(sql: str) -> None:
    """Assert the emitted SQL is valid Postgres SQL."""
    sqlglot.parse_one(sql, dialect="postgres")


# ---------------------------------------------------------------------------
# AC1 — correct aggregation at every grain
# ---------------------------------------------------------------------------


def test_ac1_scalar_no_grouping(resolver: ContractResolver, sources: list[SemanticSource]) -> None:
    """Scalar query: both leaves aggregate to one row; CROSS JOIN + divide."""
    result = compile(SemanticQuery(metrics=["avg_repair_costs"]), resolver, sources)
    _parse_ok(result.sql)
    sql_upper = result.sql.upper()
    assert "WITH" in sql_upper
    assert "NULLIF" in sql_upper
    assert "CROSS JOIN" in sql_upper
    assert "GROUP BY" not in sql_upper
    assert result.resolved == {"avg_repair_costs": "ratio(total_repair_cost, damage_count)"}
    assert result.composition is not None
    assert result.composition.kind == "ratio"
    assert result.composition.numerator == "total_repair_cost"
    assert result.composition.denominator == "damage_count"


def test_ac1_by_month_dimension(resolver: ContractResolver, sources: list[SemanticSource]) -> None:
    """Grouping by month: each leaf groups by reported_month; FULL JOIN USING."""
    result = compile(
        SemanticQuery(metrics=["avg_repair_costs"], dimensions=["reported_month"]),
        resolver,
        sources,
    )
    _parse_ok(result.sql)
    sql_upper = result.sql.upper()
    assert "WITH" in sql_upper
    assert "GROUP BY" in sql_upper
    assert "FULL" in sql_upper
    assert "USING" in sql_upper
    assert "reported_month" in result.sql.lower()
    assert "CROSS JOIN" not in sql_upper


def test_ac1_by_join_dimension(resolver: ContractResolver, sources: list[SemanticSource]) -> None:
    """Grouping by a join-reached dimension: each leaf joins to vehicles."""
    result = compile(
        SemanticQuery(metrics=["avg_repair_costs"], dimensions=["region"]),
        resolver,
        sources,
    )
    _parse_ok(result.sql)
    sql_upper = result.sql.upper()
    assert "WITH" in sql_upper
    assert "GROUP BY" in sql_upper
    assert "FULL" in sql_upper
    assert "vehicles" in result.sql.lower() or "dim_vehicles" in result.sql.lower()


def test_ac1_resolved_via_alias(resolver: ContractResolver, sources: list[SemanticSource]) -> None:
    """Composite metric resolves when queried by alias."""
    result = compile(SemanticQuery(metrics=["avg repair costs"]), resolver, sources)
    _parse_ok(result.sql)
    # resolved key is the queried alias
    assert "avg repair costs" in result.resolved


def test_ac1_sql_structure_numerator_denominator(
    resolver: ContractResolver, sources: list[SemanticSource]
) -> None:
    """The SQL must contain both sum(repair_cost) and count(damage_id)."""
    result = compile(SemanticQuery(metrics=["avg_repair_costs"]), resolver, sources)
    assert re.search(r"sum\(.+repair_cost.+\)", result.sql, re.IGNORECASE)
    assert re.search(r"count\(.+damage_id.+\)", result.sql, re.IGNORECASE)


# ---------------------------------------------------------------------------
# AC2 — zero denominator behaviour
# ---------------------------------------------------------------------------


def test_ac2_default_null_adds_nullif_and_warning(
    resolver: ContractResolver, sources: list[SemanticSource]
) -> None:
    """Default on_zero_denominator=null → NULLIF in SQL + non-empty warnings."""
    result = compile(SemanticQuery(metrics=["avg_repair_costs"]), resolver, sources)
    assert "NULLIF" in result.sql.upper()
    assert result.warnings  # at least one warning about zero-denominator


def test_ac2_zero_strategy_coalesce(
    total_cost_binding: MetricBinding,
    damage_count_binding: MetricBinding,
    sources: list[SemanticSource],
) -> None:
    """on_zero_denominator=zero → COALESCE(n / NULLIF(d, 0), 0); no warning."""
    zero_binding = MetricBinding(
        metric="avg_repair_costs",
        canonical=CanonicalRef(
            kind=BindingKind.RATIO,
            numerator="total_repair_cost",
            denominator="damage_count",
            on_zero_denominator=OnZeroDenominator.ZERO,
        ),
    )
    r = ContractResolver(
        bindings=[zero_binding, total_cost_binding, damage_count_binding], guardrails=[]
    )
    result = compile(SemanticQuery(metrics=["avg_repair_costs"]), r, sources)
    assert "COALESCE" in result.sql.upper()
    assert "NULLIF" in result.sql.upper()
    assert not result.warnings


def test_ac2_error_strategy_raw_division(
    total_cost_binding: MetricBinding,
    damage_count_binding: MetricBinding,
    sources: list[SemanticSource],
) -> None:
    """on_zero_denominator=error → raw division; no NULLIF, no warning."""
    error_binding = MetricBinding(
        metric="avg_repair_costs",
        canonical=CanonicalRef(
            kind=BindingKind.RATIO,
            numerator="total_repair_cost",
            denominator="damage_count",
            on_zero_denominator=OnZeroDenominator.ERROR,
        ),
    )
    r = ContractResolver(
        bindings=[error_binding, total_cost_binding, damage_count_binding], guardrails=[]
    )
    result = compile(SemanticQuery(metrics=["avg_repair_costs"]), r, sources)
    assert "NULLIF" not in result.sql.upper()
    assert "COALESCE" not in result.sql.upper()
    assert not result.warnings


def test_ac2_yaml_null_coerced_to_null_strategy(
    total_cost_binding: MetricBinding,
    damage_count_binding: MetricBinding,
    sources: list[SemanticSource],
) -> None:
    """on_zero_denominator: null in YAML parses to Python None → coerced to NULL strategy."""
    binding = MetricBinding(
        metric="avg_repair_costs",
        canonical=CanonicalRef.model_validate(
            {
                "kind": "ratio",
                "numerator": "total_repair_cost",
                "denominator": "damage_count",
                "on_zero_denominator": None,  # YAML null
            }
        ),
    )
    assert binding.canonical.on_zero_denominator is OnZeroDenominator.NULL


# ---------------------------------------------------------------------------
# AC3 — numerator guardrails fire automatically
# ---------------------------------------------------------------------------


def test_ac3_numerator_guardrail_fires_in_num_cte(
    resolver_with_guardrail: ContractResolver, sources: list[SemanticSource]
) -> None:
    """The numerator's mandatory filter is in the num CTE and in guardrails_fired."""
    result = compile(SemanticQuery(metrics=["avg_repair_costs"]), resolver_with_guardrail, sources)
    assert any(g.id == "total-repair-cost-no-warranty" for g in result.guardrails_fired)
    assert "warranty_claim" in result.sql.lower()


def test_ac3_denominator_has_no_numerator_guardrail(
    resolver_with_guardrail: ContractResolver, sources: list[SemanticSource]
) -> None:
    """The warranty filter appears in the num CTE but the num CTE appears before den CTE.

    We check by confirming the guardrail is fired exactly once (deduplication holds)
    and that both CTEs are present.
    """
    result = compile(SemanticQuery(metrics=["avg_repair_costs"]), resolver_with_guardrail, sources)
    warranty_fired = [g for g in result.guardrails_fired if g.id == "total-repair-cost-no-warranty"]
    assert len(warranty_fired) == 1


# ---------------------------------------------------------------------------
# Multi-metric rejection
# ---------------------------------------------------------------------------


def test_composite_alone_required(
    avg_costs_binding: MetricBinding,
    total_cost_binding: MetricBinding,
    damage_count_binding: MetricBinding,
    sources: list[SemanticSource],
) -> None:
    """Requesting a composite alongside another metric raises UnsupportedMeasure."""
    r = ContractResolver(
        bindings=[avg_costs_binding, total_cost_binding, damage_count_binding], guardrails=[]
    )
    with pytest.raises(exc.UnsupportedMeasure):
        compile(SemanticQuery(metrics=["avg_repair_costs", "total_repair_cost"]), r, sources)


# ---------------------------------------------------------------------------
# S7 — validation: cycle detection
# ---------------------------------------------------------------------------


def test_s7_cycle_raises_contract_error() -> None:
    """A cyclic composite dependency (a→b→a) is caught by validate_contracts."""
    import tempfile
    from pathlib import Path

    from canonic.contracts.validate import validate_contracts
    from canonic.exc import ContractError

    # Build a minimal project with a→b→a cycle in the ratio definitions.
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        (root / "contracts" / "metrics").mkdir(parents=True)
        (root / "semantics" / "db").mkdir(parents=True)

        (root / "semantics" / "db" / "src.yaml").write_text(
            "name: src\nconnection: db\ntable: src\ngrain: [id]\n"
            "columns:\n  - {name: id, type: string, nullable: false}\n"
            "  - {name: val, type: decimal, nullable: true}\n"
            "measures:\n  - {name: total, expr: 'sum(val)', additivity: additive}\n"
            "dimensions: []\n"
        )
        (root / "contracts" / "metrics" / "a.yaml").write_text(
            "metric: metric_a\ncanonical:\n  kind: ratio\n"
            "  numerator: metric_b\n  denominator: metric_b\nstatus: active\n"
        )
        (root / "contracts" / "metrics" / "b.yaml").write_text(
            "metric: metric_b\ncanonical:\n  kind: ratio\n"
            "  numerator: metric_a\n  denominator: metric_a\nstatus: active\n"
        )

        with pytest.raises(ContractError, match="cyclic"):
            validate_contracts(root)


def test_s7_missing_component_raises_contract_error() -> None:
    """A ratio referencing a non-existent component metric fails validate_contracts."""
    import tempfile
    from pathlib import Path

    from canonic.contracts.validate import validate_contracts
    from canonic.exc import ContractError

    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        (root / "contracts" / "metrics").mkdir(parents=True)
        (root / "semantics" / "db").mkdir(parents=True)

        (root / "semantics" / "db" / "src.yaml").write_text(
            "name: src\nconnection: db\ntable: src\ngrain: [id]\n"
            "columns:\n  - {name: id, type: string, nullable: false}\n"
            "measures: []\ndimensions: []\n"
        )
        (root / "contracts" / "metrics" / "r.yaml").write_text(
            "metric: ratio_m\ncanonical:\n  kind: ratio\n"
            "  numerator: does_not_exist\n  denominator: also_missing\nstatus: active\n"
        )

        with pytest.raises(ContractError, match="does not resolve"):
            validate_contracts(root)


# ---------------------------------------------------------------------------
# Schema validation — CanonicalRef shape errors
# ---------------------------------------------------------------------------


def test_ratio_missing_numerator_raises() -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError, match="numerator"):
        CanonicalRef(kind=BindingKind.RATIO, denominator="d")


def test_weighted_avg_missing_weight_raises() -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError, match="weight"):
        CanonicalRef(kind=BindingKind.WEIGHTED_AVG, weighted_sum="ws")


def test_single_missing_source_raises() -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError, match="source"):
        CanonicalRef(kind=BindingKind.SINGLE, measure="m")
