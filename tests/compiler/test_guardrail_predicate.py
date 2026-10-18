"""Fired guardrails say which predicate they applied and which entities they read."""

from __future__ import annotations

from typing import TYPE_CHECKING

from canonic.compiler import SemanticQuery, compile
from canonic.contracts.models import (
    AppliesTo,
    Guardrail,
    GuardrailKind,
    RestrictTo,
    Severity,
)
from canonic.contracts.resolver import ContractResolver

if TYPE_CHECKING:
    from canonic.contracts.models import MetricBinding
    from canonic.semantic.models import SemanticSource


def _fired(
    guardrail: Guardrail,
    binding: MetricBinding,
    sources: list[SemanticSource],
    context: str | None = None,
):
    resolver = ContractResolver(bindings=[binding], guardrails=[guardrail])
    result = compile(SemanticQuery(metrics=["revenue"], context=context), resolver, sources)
    (fired,) = (g for g in result.guardrails_fired if g.id == guardrail.id)
    return fired


def _guardrail(kind: GuardrailKind, **fields: object) -> Guardrail:
    return Guardrail(
        id="g",
        applies_to=AppliesTo(source="orders", measure="total_revenue"),
        kind=kind,
        severity=Severity.WARN,
        rationale="why",
        **fields,
    )


def test_mandatory_filter_reports_its_predicate_and_entities(
    refund_guardrail: Guardrail, revenue_binding: MetricBinding, sources: list[SemanticSource]
) -> None:
    fired = _fired(refund_guardrail, revenue_binding, sources)

    assert fired.predicate == "status != 'refunded'"
    assert fired.entities == (
        "warehouse_pg.orders.total_revenue",
        "warehouse_pg.orders.status",
    )


def test_entities_include_the_dimension_declared_on_a_filtered_column(
    revenue_binding: MetricBinding, sources: list[SemanticSource]
) -> None:
    guardrail = _guardrail(GuardrailKind.MANDATORY_FILTER, filter="created_at > '2024-01-01'")

    fired = _fired(guardrail, revenue_binding, sources)

    assert "warehouse_pg.orders.created_at" in fired.entities
    assert "warehouse_pg.orders.order_date" in fired.entities


def test_min_trust_reports_the_required_tier(
    revenue_binding: MetricBinding, sources: list[SemanticSource]
) -> None:
    guardrail = _guardrail(GuardrailKind.MIN_TRUST, level="provisional", context="board")

    fired = _fired(guardrail, revenue_binding, sources, context="board")

    assert fired.predicate == "trust tier >= provisional"
    assert fired.entities == ("warehouse_pg.orders.total_revenue",)


def test_restrict_source_reports_the_required_role(
    revenue_binding: MetricBinding, sources: list[SemanticSource]
) -> None:
    guardrail = _guardrail(
        GuardrailKind.RESTRICT_SOURCE, restrict_to=RestrictTo(role="final"), context="board"
    )

    fired = _fired(guardrail, revenue_binding, sources, context="board")

    assert fired.predicate == "source role = final"


def test_required_dimension_reports_the_dimension_as_an_entity(
    revenue_binding: MetricBinding, sources: list[SemanticSource]
) -> None:
    guardrail = _guardrail(GuardrailKind.REQUIRED_DIMENSION, dimension="status")

    fired = _fired(guardrail, revenue_binding, sources)

    assert fired.predicate == "group by or filter on status"
    assert "warehouse_pg.orders.status" in fired.entities


def test_declared_knowledge_refs_are_carried_through(
    revenue_binding: MetricBinding, sources: list[SemanticSource]
) -> None:
    guardrail = _guardrail(
        GuardrailKind.MANDATORY_FILTER, filter="status != 'x'", knowledge_refs=["refund-policy"]
    )

    fired = _fired(guardrail, revenue_binding, sources)

    assert fired.declared_knowledge_refs == ("refund-policy",)
    assert fired.knowledge_refs == ()
