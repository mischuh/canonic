"""Fixtures for compiler tests — an in-memory project of sources + a resolver (SPEC-E5 §9)."""

from __future__ import annotations

import pytest

from canonic.contracts.models import (
    AppliesTo,
    CanonicalRef,
    FinalityRule,
    Guardrail,
    GuardrailKind,
    MetricBinding,
    Realization,
    RestrictTo,
    Severity,
)
from canonic.contracts.resolver import ContractResolver
from canonic.semantic.models import (
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
            Dimension(name="status", column="status", label="Bestellstatus"),
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
def orders_rt() -> SemanticSource:
    """Real-time intraday orders — same schema as orders, provisional realization."""
    return SemanticSource(
        name="orders_rt",
        connection="warehouse_pg",
        table="analytics.fct_orders_rt",
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
        ],
        dimensions=[
            Dimension(name="order_date", column="created_at", granularity="day"),
            Dimension(name="status", column="status"),
        ],
        joins=[
            Join(
                to="customers",
                on="orders_rt.customer_id = customers.customer_id",
                relationship=Relationship.MANY_TO_ONE,
            ),
        ],
    )


@pytest.fixture
def accounts() -> SemanticSource:
    """Unlinked source with a 'status' dimension — alphabetically before 'orders', no join from orders."""
    return SemanticSource(
        name="accounts",
        connection="warehouse_pg",
        table="analytics.dim_accounts",
        grain=["account_id"],
        columns=[
            Column(name="account_id", type="string", nullable=False),
            Column(name="status", type="string", nullable=False),
        ],
        dimensions=[Dimension(name="status", column="status")],
    )


@pytest.fixture
def sources(
    orders: SemanticSource,
    customers: SemanticSource,
    order_items: SemanticSource,
    orders_rt: SemanticSource,
) -> list[SemanticSource]:
    return [orders, customers, order_items, orders_rt]


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


@pytest.fixture
def finality_rule() -> FinalityRule:
    """Finality rule for revenue: orders=final (watermark T-1), orders_rt=provisional."""
    return FinalityRule(
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
        coalescing="window <= watermark ? final : provisional",
        result_flag="per_row",
    )


@pytest.fixture
def finality_resolver(
    revenue_binding: MetricBinding,
    refund_guardrail: Guardrail,
    finality_rule: FinalityRule,
) -> ContractResolver:
    """Resolver with finality rule wired for revenue."""
    return ContractResolver(
        bindings=[revenue_binding],
        guardrails=[refund_guardrail],
        finality=[finality_rule],
    )


@pytest.fixture
def board_guardrail() -> Guardrail:
    return Guardrail(
        id="board-final-only",
        applies_to=AppliesTo(metric="revenue"),
        kind=GuardrailKind.RESTRICT_SOURCE,
        restrict_to=RestrictTo(role="final"),
        context="board_reporting",
        severity=Severity.ERROR,
        rationale="Board reporting requires authoritative data through T-1.",
    )


@pytest.fixture
def board_resolver(
    revenue_binding: MetricBinding,
    refund_guardrail: Guardrail,
    board_guardrail: Guardrail,
    finality_rule: FinalityRule,
) -> ContractResolver:
    """Resolver with finality rule and restrict_source guardrail for board_reporting context."""
    return ContractResolver(
        bindings=[revenue_binding],
        guardrails=[refund_guardrail, board_guardrail],
        finality=[finality_rule],
    )


@pytest.fixture
def min_trust_trusted_guardrail() -> Guardrail:
    """SPEC-E14 §7: floor unreachable in v1 (no assertion-pass/outcome signal exists yet)."""
    return Guardrail(
        id="board-reporting-trusted-only",
        applies_to=AppliesTo(metric="revenue"),
        kind=GuardrailKind.MIN_TRUST,
        level="trusted",
        context="board_reporting",
        severity=Severity.ERROR,
        rationale="Board figures must come from human-approved, final, validated definitions.",
    )


@pytest.fixture
def min_trust_provisional_guardrail() -> Guardrail:
    """A floor the fixture's revenue binding (human_curated, untested) actually meets."""
    return Guardrail(
        id="dashboards-provisional-floor",
        applies_to=AppliesTo(metric="revenue"),
        kind=GuardrailKind.MIN_TRUST,
        level="provisional",
        context="internal_dashboard",
        severity=Severity.ERROR,
        rationale="Internal dashboards require at least a provisional trust tier.",
    )


@pytest.fixture
def min_trust_resolver(
    revenue_binding: MetricBinding,
    refund_guardrail: Guardrail,
    min_trust_trusted_guardrail: Guardrail,
    min_trust_provisional_guardrail: Guardrail,
) -> ContractResolver:
    """Resolver with min_trust guardrails on both an unreachable and a met floor."""
    return ContractResolver(
        bindings=[revenue_binding],
        guardrails=[refund_guardrail, min_trust_trusted_guardrail, min_trust_provisional_guardrail],
    )
