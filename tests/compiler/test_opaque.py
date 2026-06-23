"""Compiler tests for the opaque strategy — grain-locked pre-computed values (GH-121, S5).

Acceptance criteria:
  AC1 (S5): at native grain → served directly (SQL selects the measure, groups by native grain);
            at any other grain → UNSUPPORTED_MEASURE with a rationale naming the native grain.
  population_filter (§4.5): predicate AND-ed into WHERE at native grain.
  Fanout: one_to_many join → FANOUT_UNSAFE.
  Queried alongside another metric → UNSUPPORTED_MEASURE.
"""

from __future__ import annotations

import pytest

from canon import exc
from canon.compiler import SemanticQuery, compile
from canon.contracts.models import BindingKind, CanonicalRef, MetricBinding
from canon.contracts.resolver import ContractResolver
from canon.semantic.models import Column, Dimension, Join, Measure, Relationship, SemanticSource

# ---------------------------------------------------------------------------
# Fixtures — in-memory customer_metrics project for opaque tests
# ---------------------------------------------------------------------------


@pytest.fixture
def customer_metrics_src() -> SemanticSource:
    """Pre-computed per-customer-month scores: one measure, two native-grain dimensions."""
    return SemanticSource(
        name="customer_metrics",
        connection="warehouse_pg",
        table="analytics.customer_health_scores",
        grain=["customer_id", "month"],
        columns=[
            Column(name="customer_id", type="string", nullable=False),
            Column(name="month", type="date", nullable=False),
            Column(name="region", type="string", nullable=False),
            Column(name="health_score", type="decimal", nullable=False),
        ],
        measures=[
            Measure(name="health_score", expr="health_score", additivity="non_additive"),
        ],
        dimensions=[
            Dimension(name="customer_id", column="customer_id"),
            Dimension(name="month", column="month"),
            Dimension(name="region", column="region"),
        ],
        joins=[
            Join(
                to="customer_segments",
                on="customer_metrics.customer_id = customer_segments.customer_id",
                relationship=Relationship.MANY_TO_ONE,
            ),
            Join(
                to="customer_orders",
                on="customer_metrics.customer_id = customer_orders.customer_id",
                relationship=Relationship.ONE_TO_MANY,
            ),
        ],
    )


@pytest.fixture
def customer_segments() -> SemanticSource:
    """Dimension table joined many_to_one from customer_metrics — no fanout."""
    return SemanticSource(
        name="customer_segments",
        connection="warehouse_pg",
        table="analytics.dim_customer_segments",
        grain=["customer_id"],
        columns=[
            Column(name="customer_id", type="string", nullable=False),
            Column(name="segment", type="string", nullable=False),
        ],
        dimensions=[Dimension(name="segment", column="segment")],
    )


@pytest.fixture
def customer_orders() -> SemanticSource:
    """Child table joined one_to_many from customer_metrics — fans out."""
    return SemanticSource(
        name="customer_orders",
        connection="warehouse_pg",
        table="analytics.fct_orders",
        grain=["order_id"],
        columns=[
            Column(name="order_id", type="string", nullable=False),
            Column(name="customer_id", type="string", nullable=False),
        ],
        dimensions=[],
    )


@pytest.fixture
def health_score_binding() -> MetricBinding:
    return MetricBinding(
        metric="customer_health_score",
        canonical=CanonicalRef(
            kind=BindingKind.OPAQUE,
            source="customer_metrics",
            measure="health_score",
            native_grain=["customer_id", "month"],
        ),
    )


@pytest.fixture
def health_score_filtered_binding() -> MetricBinding:
    return MetricBinding(
        metric="customer_health_score_prod",
        canonical=CanonicalRef(
            kind=BindingKind.OPAQUE,
            source="customer_metrics",
            measure="health_score",
            native_grain=["customer_id", "month"],
            population_filter="region != 'test'",
        ),
    )


@pytest.fixture
def additive_binding() -> MetricBinding:
    return MetricBinding(
        metric="some_count",
        canonical=CanonicalRef(source="customer_metrics", measure="health_score"),
    )


@pytest.fixture
def resolver(
    health_score_binding: MetricBinding,
    health_score_filtered_binding: MetricBinding,
    additive_binding: MetricBinding,
) -> ContractResolver:
    return ContractResolver(
        bindings=[health_score_binding, health_score_filtered_binding, additive_binding],
        guardrails=[],
    )


@pytest.fixture
def sources(
    customer_metrics_src: SemanticSource,
    customer_segments: SemanticSource,
    customer_orders: SemanticSource,
) -> list[SemanticSource]:
    return [customer_metrics_src, customer_segments, customer_orders]


# ---------------------------------------------------------------------------
# AC1 — served at native grain
# ---------------------------------------------------------------------------


def test_opaque_served_at_native_grain(
    resolver: ContractResolver, sources: list[SemanticSource]
) -> None:
    """At native grain the metric is served; SQL groups by native_grain dims and selects measure."""
    query = SemanticQuery(
        metrics=["customer_health_score"],
        dimensions=["customer_id", "month"],
    )
    result = compile(query, resolver, sources)

    assert result.opaque is not None
    assert set(result.opaque.native_grain) == {"customer_id", "month"}
    assert result.resolved == {"customer_health_score": "customer_metrics.health_score"}

    sql_upper = result.sql.upper()
    assert "HEALTH_SCORE" in sql_upper
    assert "GROUP BY" in sql_upper
    assert "CUSTOMER_METRICS" in sql_upper


# ---------------------------------------------------------------------------
# AC1 — rejected at wrong grain
# ---------------------------------------------------------------------------


def test_opaque_rejected_at_coarser_grain(
    resolver: ContractResolver, sources: list[SemanticSource]
) -> None:
    """Grouping by month only (dropping customer_id) → UNSUPPORTED_MEASURE."""
    query = SemanticQuery(
        metrics=["customer_health_score"],
        dimensions=["month"],
    )
    with pytest.raises(exc.UnsupportedMeasure, match="native grain"):
        compile(query, resolver, sources)


def test_opaque_rejected_at_finer_grain(
    resolver: ContractResolver, sources: list[SemanticSource]
) -> None:
    """Grouping by customer_id, month, plus an extra dim → UNSUPPORTED_MEASURE."""
    query = SemanticQuery(
        metrics=["customer_health_score"],
        dimensions=["customer_id", "month", "region"],
    )
    with pytest.raises(exc.UnsupportedMeasure, match="native grain"):
        compile(query, resolver, sources)


def test_opaque_rejected_no_dimensions(
    resolver: ContractResolver, sources: list[SemanticSource]
) -> None:
    """No grouping at all → UNSUPPORTED_MEASURE (requested grain is empty, not native grain)."""
    query = SemanticQuery(
        metrics=["customer_health_score"],
        dimensions=[],
    )
    with pytest.raises(exc.UnsupportedMeasure, match="native grain"):
        compile(query, resolver, sources)


def test_opaque_rejected_wrong_dims(
    resolver: ContractResolver, sources: list[SemanticSource]
) -> None:
    """Grouping by a completely different dim → UNSUPPORTED_MEASURE."""
    query = SemanticQuery(
        metrics=["customer_health_score"],
        dimensions=["region"],
    )
    with pytest.raises(exc.UnsupportedMeasure, match="native grain"):
        compile(query, resolver, sources)


def test_opaque_error_message_includes_native_grain(
    resolver: ContractResolver, sources: list[SemanticSource]
) -> None:
    """UNSUPPORTED_MEASURE rationale names the native grain explicitly."""
    query = SemanticQuery(
        metrics=["customer_health_score"],
        dimensions=["month"],
    )
    with pytest.raises(exc.UnsupportedMeasure) as exc_info:
        compile(query, resolver, sources)
    msg = str(exc_info.value)
    assert "customer_id" in msg
    assert "month" in msg
    assert "pre-computed" in msg


# ---------------------------------------------------------------------------
# population_filter (§4.5)
# ---------------------------------------------------------------------------


def test_opaque_population_filter_applied(
    resolver: ContractResolver, sources: list[SemanticSource]
) -> None:
    """population_filter is AND-ed into WHERE before aggregation (§4.5)."""
    query = SemanticQuery(
        metrics=["customer_health_score_prod"],
        dimensions=["customer_id", "month"],
    )
    result = compile(query, resolver, sources)

    assert "test" in result.sql.lower()
    assert result.opaque is not None


# ---------------------------------------------------------------------------
# Queried alongside another metric
# ---------------------------------------------------------------------------


def test_opaque_cannot_be_combined_with_other_metrics(
    resolver: ContractResolver, sources: list[SemanticSource]
) -> None:
    """Opaque metrics must be queried alone → UNSUPPORTED_MEASURE."""
    query = SemanticQuery(
        metrics=["customer_health_score", "some_count"],
        dimensions=["customer_id", "month"],
    )
    with pytest.raises(exc.UnsupportedMeasure, match="alone"):
        compile(query, resolver, sources)
