"""Composite metrics inherit the trust signals of their components."""

from __future__ import annotations

import pytest

from canonic.compiler import SemanticQuery, compile
from canonic.compiler.trust_inputs import trust_inputs_for_binding
from canonic.contracts.models import (
    AppliesTo,
    Assertion,
    BindingKind,
    CanonicalRef,
    Guardrail,
    GuardrailKind,
    MetricBinding,
    Severity,
)
from canonic.contracts.resolver import Binding, ContractResolver
from canonic.exc import GuardrailBlock
from canonic.feedback.assertion_history import AssertionHistory, AssertionRecord
from canonic.semantic.models import Column, Measure, SemanticSource
from canonic.trust.scorer import trust_for_compiled


@pytest.fixture
def damages() -> SemanticSource:
    return SemanticSource(
        name="damages",
        connection="warehouse_pg",
        table="fct_damages",
        grain=["damage_id"],
        columns=[
            Column(name="damage_id", type="string", nullable=False),
            Column(name="repair_cost", type="decimal", nullable=True),
        ],
        measures=[
            Measure(name="total_repair_cost", expr="sum(repair_cost)", additivity="additive"),
            Measure(name="damage_count", expr="count(damage_id)", additivity="additive"),
        ],
    )


def _bindings() -> list[MetricBinding]:
    return [
        MetricBinding(
            metric="total_repair_cost",
            canonical=CanonicalRef(source="damages", measure="total_repair_cost"),
        ),
        MetricBinding(
            metric="damage_count",
            canonical=CanonicalRef(source="damages", measure="damage_count"),
        ),
        MetricBinding(
            metric="avg_repair_costs",
            canonical=CanonicalRef(
                kind=BindingKind.RATIO,
                numerator="total_repair_cost",
                denominator="damage_count",
            ),
        ),
        MetricBinding(
            metric="cost_per_damage_per_count",
            canonical=CanonicalRef(
                kind=BindingKind.RATIO,
                numerator="avg_repair_costs",
                denominator="damage_count",
            ),
        ),
    ]


def _assertion(metric: str) -> Assertion:
    return Assertion(id=f"{metric}-check", query={"metrics": [metric]})


def _failing_numerator_history() -> AssertionHistory:
    return AssertionHistory(
        {
            "damages.total_repair_cost": AssertionRecord(
                ts="2026-01-01T00:00:00+00:00", assertion_id="cost-check", passed=False
            )
        }
    )


@pytest.fixture
def resolver() -> ContractResolver:
    return ContractResolver(
        bindings=_bindings(), guardrails=[], assertions=[_assertion("total_repair_cost")]
    )


def test_trust_inputs_include_components(
    resolver: ContractResolver, damages: SemanticSource
) -> None:
    result = compile(SemanticQuery(metrics=["avg_repair_costs"]), resolver, [damages])

    assert [(t.metric, t.owner) for t in result.trust_inputs] == [
        ("avg_repair_costs", None),
        ("total_repair_cost", "avg_repair_costs"),
        ("damage_count", "avg_repair_costs"),
    ]
    assert result.trust_inputs[1].has_assertion is True
    assert result.trust_inputs[1].binding == "damages.total_repair_cost"


def test_ratio_is_capped_by_failing_numerator(
    resolver: ContractResolver, damages: SemanticSource
) -> None:
    history = _failing_numerator_history()
    numerator = compile(SemanticQuery(metrics=["total_repair_cost"]), resolver, [damages])
    ratio = compile(SemanticQuery(metrics=["avg_repair_costs"]), resolver, [damages])

    assert trust_for_compiled(numerator, assertion_history=history).tier == "caution"

    score = trust_for_compiled(ratio, assertion_history=history)
    assert score.tier == "caution"
    assert (
        "avg_repair_costs: component total_repair_cost: assertion failed (cost-check)"
        in score.reasons
    )


def test_ratio_without_component_failures_keeps_its_own_tier(damages: SemanticSource) -> None:
    resolver = ContractResolver(bindings=_bindings(), guardrails=[])
    ratio = compile(SemanticQuery(metrics=["avg_repair_costs"]), resolver, [damages])

    score = trust_for_compiled(ratio)

    assert score.tier == "provisional"
    assert "avg_repair_costs: untested (no assertion)" in score.reasons


def test_nested_composite_walks_every_level(resolver: ContractResolver) -> None:
    resolved = resolver.resolve_metric("cost_per_damage_per_count")
    assert isinstance(resolved, Binding)

    inputs = trust_inputs_for_binding("cost_per_damage_per_count", resolved, resolver)

    assert [t.metric for t in inputs] == [
        "cost_per_damage_per_count",
        "avg_repair_costs",
        "total_repair_cost",
        "damage_count",
        "damage_count",
    ]
    assert {t.owner for t in inputs[1:]} == {"cost_per_damage_per_count"}


class TestMinTrustSeesInheritedTier:
    @pytest.fixture
    def guarded(self) -> ContractResolver:
        floor = Guardrail(
            id="board-trusted-only",
            applies_to=AppliesTo(source="damages", measure="total_repair_cost"),
            kind=GuardrailKind.MIN_TRUST,
            level="trusted",
            context="board_reporting",
            severity=Severity.ERROR,
            rationale="Board figures need a verified numerator.",
        )
        return ContractResolver(bindings=_bindings(), guardrails=[floor])

    def test_numerator_alone_is_blocked(
        self, guarded: ContractResolver, damages: SemanticSource
    ) -> None:
        with pytest.raises(GuardrailBlock):
            compile(
                SemanticQuery(metrics=["total_repair_cost"], context="board_reporting"),
                guarded,
                [damages],
            )

    def test_ratio_cannot_pass_the_floor_its_numerator_fails(
        self, guarded: ContractResolver, damages: SemanticSource
    ) -> None:
        with pytest.raises(GuardrailBlock):
            compile(
                SemanticQuery(metrics=["avg_repair_costs"], context="board_reporting"),
                guarded,
                [damages],
            )

    def test_ratio_outside_the_context_is_unaffected(
        self, guarded: ContractResolver, damages: SemanticSource
    ) -> None:
        result = compile(SemanticQuery(metrics=["avg_repair_costs"]), guarded, [damages])
        assert result.sql
