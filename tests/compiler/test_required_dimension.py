"""Compiler stage 6c acceptance tests for the required_dimension guardrail (SPEC-E5-E15 §9 S9)."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from canonic.compiler import SemanticQuery, compile
from canonic.contracts.models import (
    AppliesTo,
    Guardrail,
    GuardrailKind,
    MetricBinding,
    Severity,
)
from canonic.contracts.resolver import ContractResolver
from canonic.exc import GuardrailBlock
from canonic.semantic.models import Dimension

if TYPE_CHECKING:
    from canonic.semantic.models import SemanticSource


class TestS9RequiredDimensionAC1:
    """AC1: dimension omitted (not grouped, not filtered) → GUARDRAIL_BLOCK."""

    def test_blocks_when_dimension_absent(
        self, required_dimension_resolver: ContractResolver, sources: list[SemanticSource]
    ) -> None:
        with pytest.raises(GuardrailBlock) as exc_info:
            compile(
                SemanticQuery(metrics=["revenue"]),
                required_dimension_resolver,
                sources,
            )
        assert exc_info.value.exit_code == 8
        assert "grouped by or filtered on status" in str(exc_info.value)

    def test_candidates_name_the_missing_dimension(
        self, required_dimension_resolver: ContractResolver, sources: list[SemanticSource]
    ) -> None:
        with pytest.raises(GuardrailBlock) as exc_info:
            compile(
                SemanticQuery(metrics=["revenue"]),
                required_dimension_resolver,
                sources,
            )
        assert exc_info.value.candidates == ("status",)


class TestS9RequiredDimensionSatisfied:
    """The guardrail is satisfied when the dimension is grouped by or filtered on."""

    def test_succeeds_when_grouped_by(
        self, required_dimension_resolver: ContractResolver, sources: list[SemanticSource]
    ) -> None:
        result = compile(
            SemanticQuery(metrics=["revenue"], dimensions=["status"]),
            required_dimension_resolver,
            sources,
        )
        assert result.sql

    def test_succeeds_when_filtered_on(
        self, required_dimension_resolver: ContractResolver, sources: list[SemanticSource]
    ) -> None:
        result = compile(
            SemanticQuery(metrics=["revenue"], filters=["status = 'paid'"]),
            required_dimension_resolver,
            sources,
        )
        assert result.sql


class TestS9RequiredDimensionAlias:
    """The guardrail is satisfied via a declared dimension alias, not just its canonical name."""

    @pytest.fixture
    def orders_with_aliased_status(self, orders: SemanticSource) -> SemanticSource:
        aliased_status = Dimension(name="status", column="status", aliases=["order_status"])
        dims = [d for d in orders.dimensions if d.name != "status"] + [aliased_status]
        return orders.model_copy(update={"dimensions": dims})

    @pytest.fixture
    def sources_with_alias(
        self,
        orders_with_aliased_status: SemanticSource,
        customers: SemanticSource,
        order_items: SemanticSource,
        orders_rt: SemanticSource,
    ) -> list[SemanticSource]:
        return [orders_with_aliased_status, customers, order_items, orders_rt]

    def test_succeeds_when_grouped_by_alias(
        self,
        required_dimension_resolver: ContractResolver,
        sources_with_alias: list[SemanticSource],
    ) -> None:
        result = compile(
            SemanticQuery(metrics=["revenue"], dimensions=["order_status"]),
            required_dimension_resolver,
            sources_with_alias,
        )
        assert result.sql


class TestS9RequiredDimensionSeverityWarn:
    """severity: warn does not block; it annotates warnings[] and guardrails_fired instead."""

    @pytest.fixture
    def warn_resolver(self, revenue_binding: MetricBinding) -> ContractResolver:
        guardrail = Guardrail(
            id="revenue-requires-status",
            applies_to=AppliesTo(metric="revenue"),
            kind=GuardrailKind.REQUIRED_DIMENSION,
            dimension="status",
            severity=Severity.WARN,
            rationale="Revenue should be grouped by or filtered on status.",
        )
        return ContractResolver(bindings=[revenue_binding], guardrails=[guardrail])

    def test_warns_instead_of_blocking(
        self, warn_resolver: ContractResolver, sources: list[SemanticSource]
    ) -> None:
        result = compile(SemanticQuery(metrics=["revenue"]), warn_resolver, sources)
        assert result.sql
        assert any("revenue-requires-status" in w for w in result.warnings)

    def test_guardrails_fired_reports_warn_severity(
        self, warn_resolver: ContractResolver, sources: list[SemanticSource]
    ) -> None:
        result = compile(SemanticQuery(metrics=["revenue"]), warn_resolver, sources)
        fired = [g for g in result.guardrails_fired if g.id == "revenue-requires-status"]
        assert fired and fired[0].severity == "warn"


class TestS9RequiredDimensionContext:
    """A guardrail with no declared context applies to every query, including context=None."""

    def test_fires_with_no_query_context(
        self, required_dimension_resolver: ContractResolver, sources: list[SemanticSource]
    ) -> None:
        with pytest.raises(GuardrailBlock):
            compile(SemanticQuery(metrics=["revenue"]), required_dimension_resolver, sources)

    def test_fires_with_any_query_context(
        self, required_dimension_resolver: ContractResolver, sources: list[SemanticSource]
    ) -> None:
        with pytest.raises(GuardrailBlock):
            compile(
                SemanticQuery(metrics=["revenue"], context="some_context"),
                required_dimension_resolver,
                sources,
            )

    def test_scoped_guardrail_is_noop_outside_its_context(
        self, revenue_binding: MetricBinding, sources: list[SemanticSource]
    ) -> None:
        scoped = Guardrail(
            id="board-requires-status",
            applies_to=AppliesTo(metric="revenue"),
            kind=GuardrailKind.REQUIRED_DIMENSION,
            dimension="status",
            context="board_reporting",
            rationale="Board reporting must be grouped by or filtered on status.",
        )
        resolver = ContractResolver(bindings=[revenue_binding], guardrails=[scoped])
        result = compile(SemanticQuery(metrics=["revenue"]), resolver, sources)
        assert result.sql

    def test_scoped_guardrail_fires_inside_its_context(
        self, revenue_binding: MetricBinding, sources: list[SemanticSource]
    ) -> None:
        scoped = Guardrail(
            id="board-requires-status",
            applies_to=AppliesTo(metric="revenue"),
            kind=GuardrailKind.REQUIRED_DIMENSION,
            dimension="status",
            context="board_reporting",
            rationale="Board reporting must be grouped by or filtered on status.",
        )
        resolver = ContractResolver(bindings=[revenue_binding], guardrails=[scoped])
        with pytest.raises(GuardrailBlock):
            compile(
                SemanticQuery(metrics=["revenue"], context="board_reporting"), resolver, sources
            )


class TestS9RequiredDimensionUnresolvableName:
    """A guardrail naming a dimension not reachable from the binding's source always fires."""

    def test_blocks_when_dimension_does_not_exist(
        self, revenue_binding: MetricBinding, sources: list[SemanticSource]
    ) -> None:
        guardrail = Guardrail(
            id="revenue-requires-nonexistent",
            applies_to=AppliesTo(metric="revenue"),
            kind=GuardrailKind.REQUIRED_DIMENSION,
            dimension="does_not_exist",
            rationale="Test: dimension not declared anywhere reachable.",
        )
        resolver = ContractResolver(bindings=[revenue_binding], guardrails=[guardrail])
        with pytest.raises(GuardrailBlock):
            compile(SemanticQuery(metrics=["revenue"]), resolver, sources)


class TestS9RequiredDimensionDeterminism:
    """Two compiles of the same blocked query both raise GuardrailBlock identically."""

    def test_deterministic_block(
        self, required_dimension_resolver: ContractResolver, sources: list[SemanticSource]
    ) -> None:
        q = SemanticQuery(metrics=["revenue"])
        with pytest.raises(GuardrailBlock) as e1:
            compile(q, required_dimension_resolver, sources)
        with pytest.raises(GuardrailBlock) as e2:
            compile(q, required_dimension_resolver, sources)
        assert str(e1.value) == str(e2.value)
