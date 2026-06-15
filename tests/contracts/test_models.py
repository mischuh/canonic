"""Unit tests for canon/contracts/models.py — within-model validation."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from canon.contracts.models import (
    AppliesTo,
    Assertion,
    CanonicalRef,
    FinalityRule,
    Guardrail,
    GuardrailKind,
    MetricBinding,
    Realization,
    Severity,
    Status,
)
from canon.semantic.models import Provenance


class TestEnums:
    def test_status_values(self) -> None:
        assert Status.ACTIVE == "active"
        assert Status.DEPRECATED == "deprecated"

    def test_severity_values(self) -> None:
        assert Severity.ERROR == "error"
        assert Severity.WARN == "warn"

    def test_guardrail_kind_values(self) -> None:
        assert GuardrailKind.MANDATORY_FILTER == "mandatory_filter"
        assert GuardrailKind.REQUIRED_DIMENSION == "required_dimension"
        assert GuardrailKind.RESTRICT_SOURCE == "restrict_source"


class TestMetricBinding:
    def test_defaults(self) -> None:
        b = MetricBinding(
            metric="revenue",
            canonical=CanonicalRef(source="orders", measure="total_revenue"),
        )
        assert b.status is Status.ACTIVE
        assert b.provenance is Provenance.HUMAN_CURATED
        assert b.aliases == []
        assert b.deprecated_alternatives == []
        assert b.owner is None

    def test_alias_collision_with_metric(self) -> None:
        with pytest.raises(ValidationError, match="duplicates the metric name"):
            MetricBinding(
                metric="revenue",
                canonical=CanonicalRef(source="orders", measure="total_revenue"),
                aliases=["revenue"],
            )

    def test_valid_aliases(self) -> None:
        b = MetricBinding(
            metric="revenue",
            canonical=CanonicalRef(source="orders", measure="total_revenue"),
            aliases=["net revenue", "rev"],
        )
        assert len(b.aliases) == 2

    def test_frozen(self) -> None:
        b = MetricBinding(
            metric="revenue",
            canonical=CanonicalRef(source="orders", measure="total_revenue"),
        )
        with pytest.raises(ValidationError):
            b.metric = "other"  # type: ignore[misc]


class TestAppliesTo:
    def test_source_shape(self) -> None:
        at = AppliesTo(source="orders")
        assert at.source == "orders"
        assert at.metric is None

    def test_metric_shape(self) -> None:
        at = AppliesTo(metric="revenue")
        assert at.metric == "revenue"
        assert at.source is None

    def test_both_raises(self) -> None:
        with pytest.raises(ValidationError, match="either 'source' or 'metric'"):
            AppliesTo(source="orders", metric="revenue")

    def test_neither_raises(self) -> None:
        with pytest.raises(ValidationError, match="either 'source' or 'metric'"):
            AppliesTo()


class TestGuardrail:
    def test_mandatory_filter_requires_filter(self) -> None:
        with pytest.raises(ValidationError, match="requires a non-empty 'filter'"):
            Guardrail(
                id="bad",
                applies_to=AppliesTo(source="orders"),
                kind=GuardrailKind.MANDATORY_FILTER,
                rationale="needs filter",
            )

    def test_mandatory_filter_with_filter(self) -> None:
        g = Guardrail(
            id="ok",
            applies_to=AppliesTo(source="orders"),
            kind=GuardrailKind.MANDATORY_FILTER,
            filter="status != 'refunded'",
            rationale="Refunds are reversals.",
        )
        assert g.severity is Severity.ERROR

    def test_required_dimension_no_filter_needed(self) -> None:
        g = Guardrail(
            id="dim-guard",
            applies_to=AppliesTo(metric="revenue"),
            kind=GuardrailKind.REQUIRED_DIMENSION,
            rationale="Must group by currency.",
        )
        assert g.filter is None


class TestP1Stubs:
    def test_finality_rule_loads(self) -> None:
        rule = FinalityRule(
            metric="revenue",
            realizations=[
                Realization(source="orders", role="final", watermark="business_day - 1 day"),
                Realization(source="orders_rt", role="provisional"),
            ],
            board_only_final=True,
        )
        assert rule.metric == "revenue"
        assert len(rule.realizations) == 2

    def test_assertion_loads(self) -> None:
        a = Assertion(
            id="revenue-2025-q1",
            query={"metrics": ["revenue"], "filters": ["order_date in 2025-Q1"]},
            expect={"rows": 1, "values": {"revenue": 4218334.10}, "tolerance": 0.01},
            source_of_truth="Finance close, FY25 Q1",
        )
        assert a.id == "revenue-2025-q1"
        assert a.source_of_truth is not None
