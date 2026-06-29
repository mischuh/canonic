"""Unit tests for canon/contracts/models.py — within-model validation."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from canon.contracts.models import (
    AppliesTo,
    Assertion,
    CanonicalRef,
    Example,
    ExampleOriginKind,
    ExampleQuery,
    FinalityRule,
    Guardrail,
    GuardrailKind,
    MetricBinding,
    Realization,
    RestrictTo,
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


class TestRestrictTo:
    def test_valid_final_role(self) -> None:
        r = RestrictTo(role="final")
        assert r.role == "final"

    def test_valid_provisional_role(self) -> None:
        r = RestrictTo(role="provisional")
        assert r.role == "provisional"

    def test_invalid_role_raises(self) -> None:
        with pytest.raises(ValidationError, match="'final' or 'provisional'"):
            RestrictTo(role="authoritative")


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

    def test_restrict_source_valid(self) -> None:
        g = Guardrail(
            id="board-final-only",
            applies_to=AppliesTo(metric="revenue"),
            kind=GuardrailKind.RESTRICT_SOURCE,
            restrict_to=RestrictTo(role="final"),
            context="board_reporting",
            rationale="Board reporting requires authoritative data through T-1.",
        )
        assert g.restrict_to is not None
        assert g.restrict_to.role == "final"
        assert g.context == "board_reporting"

    def test_restrict_source_missing_restrict_to_raises(self) -> None:
        with pytest.raises(ValidationError, match="requires a 'restrict_to'"):
            Guardrail(
                id="bad-restrict",
                applies_to=AppliesTo(metric="revenue"),
                kind=GuardrailKind.RESTRICT_SOURCE,
                context="board_reporting",
                rationale="Missing restrict_to.",
            )

    def test_restrict_source_missing_context_raises(self) -> None:
        with pytest.raises(ValidationError, match="requires a non-empty 'context'"):
            Guardrail(
                id="bad-restrict",
                applies_to=AppliesTo(metric="revenue"),
                kind=GuardrailKind.RESTRICT_SOURCE,
                restrict_to=RestrictTo(role="final"),
                rationale="Missing context.",
            )


class TestP1Stubs:
    def test_finality_rule_loads(self) -> None:
        rule = FinalityRule(
            metric="revenue",
            realizations=[
                Realization(
                    source="orders",
                    role="final",
                    watermark="business_day - 1 day",
                    tz="America/New_York",
                ),
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


class TestExampleModels:
    def test_example_origin_kind_values(self) -> None:
        assert ExampleOriginKind.ASSERTION == "assertion"
        assert ExampleOriginKind.OBSERVED_QUERY == "observed_query"
        assert ExampleOriginKind.USAGE_EVIDENCE == "usage_evidence"

    def test_make_origin_observed_query(self) -> None:
        origin = Example.make_origin(ExampleOriginKind.OBSERVED_QUERY)
        assert origin == "observed_query"

    def test_make_origin_assertion(self) -> None:
        origin = Example.make_origin(ExampleOriginKind.ASSERTION, "revenue-2025-q1")
        assert origin == "assertion:revenue-2025-q1"

    def test_make_origin_usage_evidence(self) -> None:
        origin = Example.make_origin(ExampleOriginKind.USAGE_EVIDENCE, "question:412")
        assert origin == "usage_evidence:question:412"

    def test_origin_kind_property_observed(self) -> None:
        e = Example(
            query=ExampleQuery(metrics=["revenue"]),
            origin="observed_query",
            frequency=5,
        )
        assert e.origin_kind is ExampleOriginKind.OBSERVED_QUERY

    def test_origin_kind_property_assertion(self) -> None:
        e = Example(
            query=ExampleQuery(metrics=["revenue"], filters=["order_date in 2025-Q1"]),
            origin="assertion:revenue-2025-q1",
        )
        assert e.origin_kind is ExampleOriginKind.ASSERTION
        assert e.frequency is None

    def test_origin_kind_property_usage_evidence(self) -> None:
        e = Example(
            query=ExampleQuery(metrics=["revenue"]),
            origin="usage_evidence:question:412",
            frequency=38,
        )
        assert e.origin_kind is ExampleOriginKind.USAGE_EVIDENCE

    def test_example_query_defaults(self) -> None:
        q = ExampleQuery(metrics=["revenue"])
        assert q.dimensions == []
        assert q.filters == []

    def test_metric_binding_has_examples_default(self) -> None:
        b = MetricBinding(
            metric="revenue",
            canonical=CanonicalRef(source="orders", measure="total_revenue"),
        )
        assert b.examples == []

    def test_metric_binding_with_examples_round_trips(self) -> None:
        example = Example(
            query=ExampleQuery(metrics=["revenue"], dimensions=["order_date"]),
            origin="assertion:revenue-2025-q1",
        )
        b = MetricBinding(
            metric="revenue",
            canonical=CanonicalRef(source="orders", measure="total_revenue"),
            examples=[example],
        )
        raw = b.model_dump(mode="json")
        assert len(raw["examples"]) == 1
        assert raw["examples"][0]["origin"] == "assertion:revenue-2025-q1"
        assert raw["examples"][0]["frequency"] is None
