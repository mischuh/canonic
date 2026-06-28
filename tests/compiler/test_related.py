"""Tests for metadata.related (SPEC-E7/E8 §2.2 S12) — unused dimensions + sibling metrics.

Acceptance criteria (AC1-AC5) from AMENDMENT-query-suggestions.md.
"""

from __future__ import annotations

import pytest

from canon.compiler.pipeline import compile
from canon.compiler.query import SemanticQuery
from canon.contracts.models import CanonicalRef, MetricBinding, Status
from canon.contracts.resolver import ContractResolver
from canon.semantic.models import SemanticSource


@pytest.fixture
def resolver_with_sibling(revenue_binding: MetricBinding) -> ContractResolver:
    """Resolver with both 'revenue' and 'order_count' bound to orders — enables AC2 checks."""
    from canon.contracts.models import AppliesTo, Guardrail, GuardrailKind, Severity

    order_count = MetricBinding(
        metric="order_count",
        canonical=CanonicalRef(source="orders", measure="distinct_orders"),
        status=Status.ACTIVE,
    )
    guardrail = Guardrail(
        id="revenue-excludes-refunds",
        applies_to=AppliesTo(source="orders", measure="total_revenue"),
        kind=GuardrailKind.MANDATORY_FILTER,
        filter="status != 'refunded'",
        severity=Severity.ERROR,
        rationale="Refunds are reversals, not revenue.",
    )
    return ContractResolver(bindings=[revenue_binding, order_count], guardrails=[guardrail])


def test_ac1_unused_dimensions_excludes_queried(
    resolver: ContractResolver,
    sources: list[SemanticSource],
) -> None:
    """AC1: unused_dimensions excludes the queried dimension (order_date) and includes the rest."""
    query = SemanticQuery(metrics=["revenue"], dimensions=["order_date"])
    result = compile(query, resolver, sources)

    unused_names = [d.name for d in result.related.unused_dimensions]
    assert "order_date" not in unused_names
    assert "status" in unused_names
    assert "region" in unused_names


def test_ac1_unused_dimensions_sorted(
    resolver: ContractResolver,
    sources: list[SemanticSource],
) -> None:
    """AC1: unused_dimensions is sorted deterministically."""
    query = SemanticQuery(metrics=["revenue"], dimensions=["order_date"])
    result = compile(query, resolver, sources)

    names = [d.name for d in result.related.unused_dimensions]
    assert names == sorted(names)


def test_ac2_sibling_metrics_excludes_queried(
    resolver_with_sibling: ContractResolver,
    sources: list[SemanticSource],
) -> None:
    """AC2: sibling_metrics includes order_count but not the queried revenue."""
    query = SemanticQuery(metrics=["revenue"], dimensions=["order_date"])
    result = compile(query, resolver_with_sibling, sources)

    sibling_names = [m.name for m in result.related.sibling_metrics]
    assert "revenue" not in sibling_names
    assert "order_count" in sibling_names


def test_ac4_empty_lists_when_no_related(
    sources: list[SemanticSource],
) -> None:
    """AC4: a metric/source with no other dims or siblings returns empty arrays, not missing."""
    isolated_binding = MetricBinding(
        metric="isolated_metric",
        canonical=CanonicalRef(source="order_items", measure="distinct_orders"),
        status=Status.ACTIVE,
    )
    from canon.semantic.models import Column, Measure

    isolated_source = SemanticSource(
        name="order_items",
        connection="warehouse_pg",
        table="analytics.fct_order_items",
        grain=["item_id"],
        columns=[Column(name="item_id", type="string", nullable=False)],
        measures=[
            Measure(
                name="distinct_orders", expr="count(distinct item_id)", additivity="non_additive"
            )
        ],
        dimensions=[],
    )
    resolver = ContractResolver(bindings=[isolated_binding], guardrails=[])
    query = SemanticQuery(metrics=["isolated_metric"])
    result = compile(query, resolver, [isolated_source])

    assert result.related.unused_dimensions == []
    assert result.related.sibling_metrics == []
    # Field must be present (not None)
    assert result.related is not None


def test_ac5_determinism(
    resolver_with_sibling: ContractResolver,
    sources: list[SemanticSource],
) -> None:
    """AC5: repeated identical queries return related in identical order."""
    query = SemanticQuery(metrics=["revenue"], dimensions=["order_date"])
    result1 = compile(query, resolver_with_sibling, sources)
    result2 = compile(query, resolver_with_sibling, sources)

    assert result1.related == result2.related


def test_filter_tokens_excluded_from_unused_dims(
    resolver: ContractResolver,
    sources: list[SemanticSource],
) -> None:
    """Dimensions referenced in filters are excluded from unused_dimensions."""
    query = SemanticQuery(
        metrics=["revenue"],
        dimensions=["order_date"],
        filters=["status = 'active'"],
    )
    result = compile(query, resolver, sources)

    unused_names = [d.name for d in result.related.unused_dimensions]
    assert "status" not in unused_names


def test_unused_dimensions_propagates_label(
    resolver: ContractResolver,
    sources: list[SemanticSource],
) -> None:
    """Dimension labels are propagated to unused_dimensions entries."""
    query = SemanticQuery(metrics=["revenue"], dimensions=["order_date"])
    result = compile(query, resolver, sources)

    status_dim = next((d for d in result.related.unused_dimensions if d.name == "status"), None)
    assert status_dim is not None
    assert status_dim.label == "Bestellstatus"


def test_related_capped_at_five(
    sources: list[SemanticSource],
) -> None:
    """unused_dimensions is capped at 5 even when more are available."""
    from canon.semantic.models import Column, Dimension, Measure

    many_dims_source = SemanticSource(
        name="wide",
        connection="wh",
        table="wh.wide",
        grain=["id"],
        columns=[Column(name="id", type="string", nullable=False)]
        + [Column(name=f"col_{i}", type="string", nullable=False) for i in range(10)],
        measures=[Measure(name="cnt", expr="count(id)", additivity="additive")],
        dimensions=[Dimension(name=f"dim_{i}", column=f"col_{i}") for i in range(10)],
    )
    binding = MetricBinding(
        metric="wide_cnt",
        canonical=CanonicalRef(source="wide", measure="cnt"),
        status=Status.ACTIVE,
    )
    resolver = ContractResolver(bindings=[binding], guardrails=[])
    result = compile(SemanticQuery(metrics=["wide_cnt"]), resolver, [many_dims_source])

    assert len(result.related.unused_dimensions) <= 5
