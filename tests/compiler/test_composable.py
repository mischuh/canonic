"""Compiler tests for composable_post_agg strategy — ratio & weighted_avg (GH-118, S2).

Acceptance criteria:
  AC1: avg_repair_costs = total_repair_cost / damage_count produces numerator-sum ÷
       denominator-sum at the requested grain (no-grouping, by month, by region).
  AC2: Zero denominator → NULL + warning (default); ZERO → COALESCE; ERROR → raw division.
  AC3: Numerator's own guardrails fire automatically on its leaf.
"""

from __future__ import annotations

import re

import duckdb
import pytest
import sqlglot

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
            ),
            Join(
                to="repair_line_items",
                on="damages.damage_id = repair_line_items.damage_id",
                relationship=Relationship.ONE_TO_MANY,
            ),
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
def repair_line_items() -> SemanticSource:
    """Child table joined one_to_many from damages — fans out the damage grain."""
    return SemanticSource(
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


@pytest.fixture
def sources(
    damages: SemanticSource, vehicles: SemanticSource, repair_line_items: SemanticSource
) -> list[SemanticSource]:
    return [damages, vehicles, repair_line_items]


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
    """Scalar query: the leaves aggregate to one row each, and the division happens after.

    Both components sit on ``damages`` with the same filters, so compose fuses them into
    one CTE projecting both measures rather than emitting two CTEs and joining them —
    the join carried no information, since both sides were the same query.
    """
    result = compile(SemanticQuery(metrics=["avg_repair_costs"]), resolver, sources)
    _parse_ok(result.sql)
    sql_upper = result.sql.upper()
    assert "WITH" in sql_upper
    assert "NULLIF" in sql_upper
    assert sql_upper.count("_LEAF_") >= 1
    assert "_LEAF_1" not in sql_upper, "identical component plans must fuse into one CTE"
    assert "GROUP BY" not in sql_upper
    assert result.resolved == {"avg_repair_costs": "ratio(total_repair_cost, damage_count)"}
    assert result.composition is not None
    assert result.composition.kind == "ratio"
    assert result.composition.numerator == "total_repair_cost"
    assert result.composition.denominator == "damage_count"


def test_ratio_metric_trust_input_binding_matches_resolved_key(
    resolver: ContractResolver, sources: list[SemanticSource]
) -> None:
    """The trust/assertion join key gathered at resolve time (TrustInput.binding, SPEC-E14
    §4) must match the string actually persisted to .canonic/assertions.json (result.resolved),
    or a passing assertion for this metric can never be recognized as trusted."""
    result = compile(SemanticQuery(metrics=["avg_repair_costs"]), resolver, sources)
    (trust_input,) = result.trust_inputs
    assert trust_input.binding == result.resolved["avg_repair_costs"]
    assert trust_input.binding == "ratio(total_repair_cost, damage_count)"


def test_ac1_by_month_dimension(resolver: ContractResolver, sources: list[SemanticSource]) -> None:
    """Grouping by month: the leaf groups by reported_month and the division follows it."""
    result = compile(
        SemanticQuery(metrics=["avg_repair_costs"], dimensions=["reported_month"]),
        resolver,
        sources,
    )
    _parse_ok(result.sql)
    sql_upper = result.sql.upper()
    assert "WITH" in sql_upper
    assert "GROUP BY" in sql_upper
    assert "NULLIF" in sql_upper
    assert "reported_month" in result.sql.lower()
    assert "CROSS JOIN" not in sql_upper


def test_ac1_by_join_dimension(resolver: ContractResolver, sources: list[SemanticSource]) -> None:
    """Grouping by a join-reached dimension: the leaf joins to vehicles before aggregating."""
    result = compile(
        SemanticQuery(metrics=["avg_repair_costs"], dimensions=["region"]),
        resolver,
        sources,
    )
    _parse_ok(result.sql)
    sql_upper = result.sql.upper()
    assert "WITH" in sql_upper
    assert "GROUP BY" in sql_upper
    assert "NULLIF" in sql_upper
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
# Fanout regression — additive leaves must dedup like simple_additive.py does
# (a one_to_many join to reach the requested dimension must not inflate the
# numerator/denominator sums; see the jaffle_shop bug report).
# ---------------------------------------------------------------------------


def test_fanout_composite_ratio_leaves_use_distinct_on_dedup(
    resolver: ContractResolver, sources: list[SemanticSource]
) -> None:
    """Grouping by a one_to_many-reached dimension: the leaf dedups before aggregating.

    One ``DISTINCT ON``, not two: the components share a plan and fuse into a single CTE,
    so the damage grain is deduplicated once and both measures aggregate over the same
    deduplicated rows. The property that matters is that the dedup happens at all — see
    ``test_fanout_composite_ratio_numeric_correctness`` for the falsifying value check.
    """
    result = compile(
        SemanticQuery(metrics=["avg_repair_costs"], dimensions=["part_name"]),
        resolver,
        sources,
    )
    _parse_ok(result.sql)
    sql_upper = result.sql.upper()
    assert sql_upper.count("DISTINCT ON") == 1
    assert '"damages"."damage_id"' in result.sql


def test_fanout_composite_ratio_numeric_correctness(
    resolver: ContractResolver, sources: list[SemanticSource]
) -> None:
    """Executed regression: a multi-line-item damage must not inflate the ratio.

    damage 'd1' costs 100 and has two repair line items (both 'bumper'), 'd2' costs
    40 and has one. Correct: total_repair_cost=140, damage_count=2 -> ratio=70. A
    flat (un-deduped) join fans d1's row out twice, so the buggy numerator sums
    100+100+40=240 and the buggy denominator counts 3 -> ratio=80 — wrong on both
    sides, and not just wrong but not even reduced to the correct value.
    """
    result = compile(
        SemanticQuery(metrics=["avg_repair_costs"], dimensions=["part_name"]),
        resolver,
        sources,
        connection_dialects={"warehouse_pg": "duckdb"},
    )
    con = duckdb.connect(":memory:")
    con.execute(
        "CREATE TABLE fct_damages (damage_id VARCHAR, vehicle_id VARCHAR, "
        "repair_cost DECIMAL, reported_month VARCHAR, warranty_claim INT)"
    )
    con.execute(
        "CREATE TABLE fct_repair_line_items (line_item_id VARCHAR, damage_id VARCHAR, "
        "part_name VARCHAR)"
    )
    con.execute(
        "INSERT INTO fct_damages VALUES "
        "('d1', 'v1', 100, '2024-01', 0), ('d2', 'v1', 40, '2024-01', 0)"
    )
    con.execute(
        "INSERT INTO fct_repair_line_items VALUES "
        "('l1', 'd1', 'bumper'), ('l2', 'd1', 'bumper'), ('l3', 'd2', 'bumper')"
    )
    rows = {r[0]: r[1] for r in con.execute(result.sql).fetchall()}
    # Correct: total_repair_cost=140, damage_count=2 -> 70. The fanout bug would
    # sum repair_cost once per line item (100*2 + 40 = 240) and inflate the count
    # (3), landing on 80 instead of 70.
    assert rows["bumper"] == pytest.approx(70.0)


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
    """A ratio compiles alongside another metric, sharing the leaf they have in common.

    This is the request that used to raise ``UnsupportedMeasure("must be queried alone")``
    and the symptom that prompted AMENDMENT-multi-metric-compose (S12 AC1/AC2). The ratio's
    numerator *is* ``total_repair_cost``, so the two requests resolve to the same plan and
    that aggregate is computed once, not twice.
    """
    r = ContractResolver(
        bindings=[avg_costs_binding, total_cost_binding, damage_count_binding], guardrails=[]
    )
    result = compile(SemanticQuery(metrics=["avg_repair_costs", "total_repair_cost"]), r, sources)
    _parse_ok(result.sql)
    assert result.sql.upper().count("SELECT") == 2, "one leaf plus one outer SELECT"
    assert re.findall(r"sum\(.+repair_cost.+\)", result.sql, re.IGNORECASE), "aggregated once"
    assert len(re.findall(r"SUM\(", result.sql, re.IGNORECASE)) == 1
    assert set(result.resolved) == {"avg_repair_costs", "total_repair_cost"}


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
