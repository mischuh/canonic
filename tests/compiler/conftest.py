"""Fixtures for compiler tests — an in-memory project of sources + a resolver (SPEC-E5 §9)."""

from __future__ import annotations

import pytest

from canon.contracts.models import (
    AppliesTo,
    CanonicalRef,
    Guardrail,
    GuardrailKind,
    MetricBinding,
    Severity,
)
from canon.contracts.resolver import ContractResolver
from canon.semantic.models import (
    Column,
    Dimension,
    Join,
    Measure,
    Relationship,
    SemanticSource,
)


@pytest.fixture
def orders() -> SemanticSource:
    """Fact at order grain — additive revenue, a declared non-additive measure, two dims."""
    return SemanticSource(
        name="orders",
        connection="warehouse_pg",
        table="analytics.fct_orders",
        grain=["order_id"],
        columns=[
            Column(name="order_id", type="string", nullable=False),
            Column(name="customer_id", type="string", nullable=False),
            Column(name="status", type="string", nullable=False),
            Column(name="amount", type="decimal", nullable=False),
            Column(name="created_at", type="timestamp", nullable=False),
        ],
        measures=[
            Measure(name="total_revenue", expr="sum(amount)", additivity="additive"),
            Measure(
                name="distinct_orders", expr="count(distinct order_id)", additivity="non_additive"
            ),
        ],
        dimensions=[
            Dimension(name="order_date", column="created_at", granularity="day"),
            Dimension(name="status", column="status"),
        ],
        joins=[
            Join(
                to="customers",
                on="orders.customer_id = customers.customer_id",
                relationship=Relationship.MANY_TO_ONE,
            ),
            Join(
                to="order_items",
                on="orders.order_id = order_items.order_id",
                relationship=Relationship.ONE_TO_MANY,
            ),
        ],
    )


@pytest.fixture
def customers() -> SemanticSource:
    """Dimension table joined many_to_one from orders — no fanout."""
    return SemanticSource(
        name="customers",
        connection="warehouse_pg",
        table="analytics.dim_customers",
        grain=["customer_id"],
        columns=[
            Column(name="customer_id", type="string", nullable=False),
            Column(name="region", type="string", nullable=False),
        ],
        dimensions=[Dimension(name="region", column="region")],
    )


@pytest.fixture
def order_items() -> SemanticSource:
    """Child table joined one_to_many from orders — fans out the order grain."""
    return SemanticSource(
        name="order_items",
        connection="warehouse_pg",
        table="analytics.fct_order_items",
        grain=["item_id"],
        columns=[
            Column(name="item_id", type="string", nullable=False),
            Column(name="order_id", type="string", nullable=False),
            Column(name="sku", type="string", nullable=False),
        ],
        dimensions=[Dimension(name="sku", column="sku")],
    )


@pytest.fixture
def sources(
    orders: SemanticSource,
    customers: SemanticSource,
    order_items: SemanticSource,
) -> list[SemanticSource]:
    return [orders, customers, order_items]


@pytest.fixture
def revenue_binding() -> MetricBinding:
    return MetricBinding(
        metric="revenue",
        canonical=CanonicalRef(source="orders", measure="total_revenue"),
        aliases=["rev", "net revenue"],
    )


@pytest.fixture
def refund_guardrail() -> Guardrail:
    return Guardrail(
        id="revenue-excludes-refunds",
        applies_to=AppliesTo(source="orders", measure="total_revenue"),
        kind=GuardrailKind.MANDATORY_FILTER,
        filter="status != 'refunded'",
        severity=Severity.ERROR,
        rationale="Refunds are reversals, not revenue.",
    )


@pytest.fixture
def resolver(revenue_binding: MetricBinding, refund_guardrail: Guardrail) -> ContractResolver:
    """One canonical revenue binding + the excludes-refunds guardrail, plus a uniques metric."""
    uniques = MetricBinding(
        metric="distinct_order_count",
        canonical=CanonicalRef(source="orders", measure="distinct_orders"),
    )
    return ContractResolver(bindings=[revenue_binding, uniques], guardrails=[refund_guardrail])
