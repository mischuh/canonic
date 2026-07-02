"""Compiler tests for opaque strategy — grain-locked pre-computed values (GH-121, S5).

Acceptance criteria:
  AC1 (S5): customer_health_score at customer_id × month → direct lookup, no re-aggregation.
            SQL contains the measure column, NO GROUP BY, NO aggregate function.
  AC1 (S5): at any other grain → UNSUPPORTED_MEASURE with rationale naming the native grain.
  S7 AC1: a ratio/weighted_avg referencing an opaque component fails validation.
  population_filter: predicate AND-ed into the WHERE of the native-grain lookup (§4.5).
  Model validation: opaque binding requires source, measure, and non-empty native_grain.
"""

from __future__ import annotations

import pytest
import sqlglot

from canonic import exc
from canonic.compiler import SemanticQuery, compile
from canonic.contracts.models import BindingKind, CanonicalRef, MetricBinding
from canonic.contracts.resolver import ContractResolver
from canonic.semantic.models import Column, Dimension, Measure, SemanticSource

# ---------------------------------------------------------------------------
# Fixtures — customer_metrics pre-computed scores table
# ---------------------------------------------------------------------------


@pytest.fixture
def customer_metrics_source() -> SemanticSource:
    """Pre-computed health scores at customer_id × month grain."""
    return SemanticSource(
        name="customer_metrics",
        connection="warehouse_pg",
        table="analytics.customer_metrics_scores",
        grain=["customer_id", "month"],
        columns=[
            Column(name="customer_id", type="string", nullable=False),
            Column(name="month", type="date", nullable=False),
            Column(name="health_score", type="decimal", nullable=False),
        ],
        measures=[
            Measure(name="health_score", expr="health_score", additivity="additive"),
        ],
        dimensions=[
            Dimension(name="customer_id", column="customer_id"),
            Dimension(name="month", column="month"),
        ],
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
def opaque_resolver(health_score_binding: MetricBinding) -> ContractResolver:
    return ContractResolver(bindings=[health_score_binding], guardrails=[])


def _parse_ok(sql: str) -> None:
    sqlglot.parse_one(sql, dialect="postgres")


# ---------------------------------------------------------------------------
# AC1 — served at native grain; direct lookup with no re-aggregation
# ---------------------------------------------------------------------------


def test_ac1_at_native_grain(
    opaque_resolver: ContractResolver,
    customer_metrics_source: SemanticSource,
) -> None:
    """At native grain → direct SELECT of the measure; no GROUP BY, no SUM/COUNT/AVG."""
    result = compile(
        SemanticQuery(
            metrics=["customer_health_score"],
            dimensions=["customer_id", "month"],
        ),
        opaque_resolver,
        [customer_metrics_source],
    )
    _parse_ok(result.sql)
    sql_upper = result.sql.upper()
    assert "HEALTH_SCORE" in sql_upper
    assert "GROUP BY" not in sql_upper
    assert "SUM(" not in sql_upper
    assert "COUNT(" not in sql_upper
    assert "AVG(" not in sql_upper
    assert result.opaque is not None
    assert result.opaque.source == "customer_metrics"
    assert result.opaque.measure == "health_score"
    assert set(result.opaque.native_grain) == {"customer_id", "month"}
    assert result.resolved == {"customer_health_score": "opaque(customer_metrics.health_score)"}


def test_ac1_native_grain_order_independent(
    opaque_resolver: ContractResolver,
    customer_metrics_source: SemanticSource,
) -> None:
    """Grain match is set equality — dimension order in the query does not matter."""
    result = compile(
        SemanticQuery(
            metrics=["customer_health_score"],
            dimensions=["month", "customer_id"],
        ),
        opaque_resolver,
        [customer_metrics_source],
    )
    _parse_ok(result.sql)
    assert result.opaque is not None


# ---------------------------------------------------------------------------
# AC1 — rejected at any other grain with UNSUPPORTED_MEASURE + rationale
# ---------------------------------------------------------------------------


def test_ac1_rejects_coarser_grain(
    opaque_resolver: ContractResolver,
    customer_metrics_source: SemanticSource,
) -> None:
    """At coarser grain (missing month) → UNSUPPORTED_MEASURE with native grain in rationale."""
    with pytest.raises(exc.UnsupportedMeasure) as exc_info:
        compile(
            SemanticQuery(
                metrics=["customer_health_score"],
                dimensions=["customer_id"],
            ),
            opaque_resolver,
            [customer_metrics_source],
        )
    msg = str(exc_info.value).lower()
    assert "native grain" in msg
    assert "customer_id" in msg
    assert "month" in msg


def test_ac1_rejects_different_grain(
    opaque_resolver: ContractResolver,
    customer_metrics_source: SemanticSource,
) -> None:
    """At a completely different grain → UNSUPPORTED_MEASURE."""
    with pytest.raises(exc.UnsupportedMeasure):
        compile(
            SemanticQuery(
                metrics=["customer_health_score"],
                dimensions=["month"],
            ),
            opaque_resolver,
            [customer_metrics_source],
        )


def test_ac1_rejects_superset_grain(
    opaque_resolver: ContractResolver,
    customer_metrics_source: SemanticSource,
) -> None:
    """Even a strict superset of native_grain (extra dim) → UNSUPPORTED_MEASURE.

    Opaque requires exact match — no extra dimensions allowed (confirmed design decision).
    """
    source_with_region = SemanticSource(
        name="customer_metrics",
        connection="warehouse_pg",
        table="analytics.customer_metrics_scores",
        grain=["customer_id", "month"],
        columns=[
            Column(name="customer_id", type="string", nullable=False),
            Column(name="month", type="date", nullable=False),
            Column(name="region", type="string", nullable=False),
            Column(name="health_score", type="decimal", nullable=False),
        ],
        measures=[
            Measure(name="health_score", expr="health_score", additivity="additive"),
        ],
        dimensions=[
            Dimension(name="customer_id", column="customer_id"),
            Dimension(name="month", column="month"),
            Dimension(name="region", column="region"),
        ],
    )
    with pytest.raises(exc.UnsupportedMeasure):
        compile(
            SemanticQuery(
                metrics=["customer_health_score"],
                dimensions=["customer_id", "month", "region"],
            ),
            opaque_resolver,
            [source_with_region],
        )


def test_ac1_rejects_scalar_query(
    opaque_resolver: ContractResolver,
    customer_metrics_source: SemanticSource,
) -> None:
    """Scalar query (no dimensions) → UNSUPPORTED_MEASURE."""
    with pytest.raises(exc.UnsupportedMeasure):
        compile(
            SemanticQuery(metrics=["customer_health_score"]),
            opaque_resolver,
            [customer_metrics_source],
        )


# ---------------------------------------------------------------------------
# Multi-metric rejection
# ---------------------------------------------------------------------------


def test_opaque_must_be_queried_alone(
    opaque_resolver: ContractResolver,
    customer_metrics_source: SemanticSource,
) -> None:
    """Querying an opaque metric alongside another metric → UnsupportedMeasure."""
    extra_binding = MetricBinding(
        metric="revenue",
        canonical=CanonicalRef(
            kind=BindingKind.SINGLE,
            source="customer_metrics",
            measure="health_score",
        ),
    )
    resolver = ContractResolver(
        bindings=[
            MetricBinding(
                metric="customer_health_score",
                canonical=CanonicalRef(
                    kind=BindingKind.OPAQUE,
                    source="customer_metrics",
                    measure="health_score",
                    native_grain=["customer_id", "month"],
                ),
            ),
            extra_binding,
        ],
        guardrails=[],
    )
    with pytest.raises(exc.UnsupportedMeasure):
        compile(
            SemanticQuery(
                metrics=["customer_health_score", "revenue"],
                dimensions=["customer_id", "month"],
            ),
            resolver,
            [customer_metrics_source],
        )


# ---------------------------------------------------------------------------
# population_filter (§4.5) — applied to the native-grain lookup WHERE
# ---------------------------------------------------------------------------


def test_population_filter_in_native_grain_lookup(
    customer_metrics_source: SemanticSource,
) -> None:
    """population_filter appears in WHERE of the direct lookup before the measure."""
    binding = MetricBinding(
        metric="customer_health_score",
        canonical=CanonicalRef(
            kind=BindingKind.OPAQUE,
            source="customer_metrics",
            measure="health_score",
            native_grain=["customer_id", "month"],
            population_filter="customer_id NOT IN (SELECT customer_id FROM test_accounts)",
        ),
    )
    resolver = ContractResolver(bindings=[binding], guardrails=[])
    result = compile(
        SemanticQuery(
            metrics=["customer_health_score"],
            dimensions=["customer_id", "month"],
        ),
        resolver,
        [customer_metrics_source],
    )
    _parse_ok(result.sql)
    sql_upper = result.sql.upper()
    assert "TEST_ACCOUNTS" in sql_upper
    assert "GROUP BY" not in sql_upper  # still a raw lookup, not aggregated


# ---------------------------------------------------------------------------
# S7 AC1 — opaque component rejected inside ratio/weighted_avg (validation)
# ---------------------------------------------------------------------------


def test_s7_ac1_opaque_component_in_ratio_rejected() -> None:
    """A ratio metric referencing an opaque component → ContractError at validation."""
    from canonic.contracts.validate import _validate_composite_binding

    opaque_binding = MetricBinding(
        metric="health_score",
        canonical=CanonicalRef(
            kind=BindingKind.OPAQUE,
            source="customer_metrics",
            measure="health_score",
            native_grain=["customer_id", "month"],
        ),
    )
    simple_binding = MetricBinding(
        metric="customer_count",
        canonical=CanonicalRef(
            kind=BindingKind.SINGLE,
            source="customer_metrics",
            measure="health_score",
        ),
    )
    ratio_binding = MetricBinding(
        metric="bad_ratio",
        canonical=CanonicalRef(
            kind=BindingKind.RATIO,
            numerator="customer_count",
            denominator="health_score",
        ),
    )
    with pytest.raises(exc.ContractError) as exc_info:
        _validate_composite_binding(
            ratio_binding,
            [opaque_binding, simple_binding, ratio_binding],
            source_measures={"customer_metrics": {"health_score"}},
        )
    msg = str(exc_info.value).lower()
    assert "opaque" in msg


def test_s7_ac1_opaque_component_as_numerator_rejected() -> None:
    """Opaque as numerator → ContractError."""
    from canonic.contracts.validate import _validate_composite_binding

    opaque_binding = MetricBinding(
        metric="health_score",
        canonical=CanonicalRef(
            kind=BindingKind.OPAQUE,
            source="customer_metrics",
            measure="health_score",
            native_grain=["customer_id", "month"],
        ),
    )
    simple_binding = MetricBinding(
        metric="customer_count",
        canonical=CanonicalRef(
            kind=BindingKind.SINGLE,
            source="customer_metrics",
            measure="health_score",
        ),
    )
    ratio_binding = MetricBinding(
        metric="bad_ratio",
        canonical=CanonicalRef(
            kind=BindingKind.RATIO,
            numerator="health_score",
            denominator="customer_count",
        ),
    )
    with pytest.raises(exc.ContractError) as exc_info:
        _validate_composite_binding(
            ratio_binding,
            [opaque_binding, simple_binding, ratio_binding],
            source_measures={"customer_metrics": {"health_score"}},
        )
    assert "opaque" in str(exc_info.value).lower()


# ---------------------------------------------------------------------------
# Model validation — CanonicalRef shape errors
# ---------------------------------------------------------------------------


def test_opaque_requires_source() -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        CanonicalRef(
            kind=BindingKind.OPAQUE,
            measure="health_score",
            native_grain=["customer_id", "month"],
        )


def test_opaque_requires_measure() -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        CanonicalRef(
            kind=BindingKind.OPAQUE,
            source="customer_metrics",
            native_grain=["customer_id", "month"],
        )


def test_opaque_requires_native_grain() -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        CanonicalRef(
            kind=BindingKind.OPAQUE,
            source="customer_metrics",
            measure="health_score",
        )


def test_opaque_requires_nonempty_native_grain() -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        CanonicalRef(
            kind=BindingKind.OPAQUE,
            source="customer_metrics",
            measure="health_score",
            native_grain=[],
        )


def test_opaque_valid_construction() -> None:
    """Valid opaque binding constructs without error."""
    ref = CanonicalRef(
        kind=BindingKind.OPAQUE,
        source="customer_metrics",
        measure="health_score",
        native_grain=["customer_id", "month"],
    )
    assert ref.kind is BindingKind.OPAQUE
    assert ref.native_grain == ["customer_id", "month"]
