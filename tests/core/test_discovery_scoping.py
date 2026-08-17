"""Discovery is scoped through the caller's effective policy (SPEC-E12 §6, S15).

Covers list_metrics/get_overview/describe_metric/resolve_metric — no tenancy policy
involved here, purely the RBAC (roles.yaml) half, exercised directly against
CanonicService so these run without a live database connection.
"""

from __future__ import annotations

import pytest

from canonic.config import CanonicConfig
from canonic.contracts.models import (
    AllowDenyPolicy,
    CanonicalRef,
    MetricBinding,
    RoleDef,
    RolePolicy,
    Status,
)
from canonic.contracts.principal import Principal
from canonic.contracts.resolver import ContractResolver
from canonic.core.service import CanonicService
from canonic.exc import Unresolved
from canonic.semantic.models import Column, Dimension, Measure, SemanticSource

_DC_CONFIG = {
    "version": 1,
    "project": {"name": "test", "default_connection": "warehouse_pg"},
    "connections": [
        {
            "id": "warehouse_pg",
            "type": "postgres",
            "params": {"host": "localhost", "port": 5432, "dbname": "testdb", "user": "test"},
            "credentials_ref": "env:PG_PASSWORD",
        }
    ],
    "llm": {"provider": "openai_compatible", "base_url": "http://localhost/v1", "model": "llama3"},
}


@pytest.fixture
def orders_source() -> SemanticSource:
    return SemanticSource(
        name="orders",
        connection="warehouse_pg",
        table="analytics.fct_orders",
        grain=["order_id"],
        columns=[
            Column(name="order_id", type="string", nullable=False),
            Column(name="amount", type="decimal", nullable=False),
            Column(name="status", type="string", nullable=False),
        ],
        measures=[
            Measure(name="total_revenue", expr="sum(amount)", additivity="additive"),
            Measure(name="total_cost", expr="sum(amount)", additivity="additive"),
        ],
        dimensions=[Dimension(name="status", column="status")],
    )


@pytest.fixture
def revenue_binding() -> MetricBinding:
    return MetricBinding(
        metric="revenue",
        canonical=CanonicalRef(source="orders", measure="total_revenue"),
        status=Status.ACTIVE,
    )


@pytest.fixture
def cost_binding() -> MetricBinding:
    """Not in ``merchant_viewer``'s allow list — the denied metric these tests probe."""
    return MetricBinding(
        metric="cost",
        canonical=CanonicalRef(source="orders", measure="total_cost"),
        status=Status.ACTIVE,
    )


@pytest.fixture
def role_policy() -> RolePolicy:
    return RolePolicy(
        schema_="roles/v1",
        claim="roles",
        default_role="merchant_viewer",
        roles={
            "merchant_viewer": RoleDef(metrics=AllowDenyPolicy(allow=["revenue"])),
            "platform_analyst": RoleDef(metrics=AllowDenyPolicy(allow=["*"])),
        },
    )


@pytest.fixture
def scoped_service(
    revenue_binding: MetricBinding,
    cost_binding: MetricBinding,
    role_policy: RolePolicy,
    orders_source: SemanticSource,
    monkeypatch: pytest.MonkeyPatch,
) -> CanonicService:
    monkeypatch.setenv("PG_PASSWORD", "testpassword")
    resolver = ContractResolver(
        bindings=[revenue_binding, cost_binding], guardrails=[], roles=role_policy
    )
    config = CanonicConfig.model_validate(_DC_CONFIG)
    return CanonicService(config=config, resolver=resolver, sources=[orders_source])


_VIEWER = Principal(tenant=None, roles=("merchant_viewer",))
_ANALYST = Principal(tenant=None, roles=("platform_analyst",))


class TestListMetrics:
    def test_omits_denied_metric(self, scoped_service: CanonicService) -> None:
        names = {s.metric for s in scoped_service.list_metrics(principal=_VIEWER)}
        assert names == {"revenue"}

    def test_analyst_sees_everything(self, scoped_service: CanonicService) -> None:
        names = {s.metric for s in scoped_service.list_metrics(principal=_ANALYST)}
        assert names == {"revenue", "cost"}

    def test_no_principal_applies_default_role(self, scoped_service: CanonicService) -> None:
        """No principal still applies ``default_role`` — matching the compiler's stage 0,
        which never treats a missing principal as unrestricted once a role policy loads."""
        names = {s.metric for s in scoped_service.list_metrics()}
        assert names == {"revenue"}


class TestGetOverview:
    def test_omits_denied_metric_and_empty_domain(self, scoped_service: CanonicService) -> None:
        overview = scoped_service.get_overview(principal=_VIEWER)
        all_metrics = {m.name for g in overview.domains for m in g.metrics}
        assert all_metrics == {"revenue"}

    def test_analyst_sees_everything(self, scoped_service: CanonicService) -> None:
        overview = scoped_service.get_overview(principal=_ANALYST)
        all_metrics = {m.name for g in overview.domains for m in g.metrics}
        assert all_metrics == {"revenue", "cost"}


class TestDescribeMetric:
    def test_allowed_metric_describes_normally(self, scoped_service: CanonicService) -> None:
        detail = scoped_service.describe_metric("revenue", principal=_VIEWER)
        assert detail.metric == "revenue"

    def test_denied_metric_raises_unresolved_shape(self, scoped_service: CanonicService) -> None:
        """S15 AC3: same UNRESOLVED shape as a name that does not exist at all — never a
        distinct forbidden error that would leak whether 'cost' exists."""
        with pytest.raises(Unresolved) as denied_exc:
            scoped_service.describe_metric("cost", principal=_VIEWER)
        with pytest.raises(Unresolved) as missing_exc:
            scoped_service.describe_metric("does_not_exist", principal=_VIEWER)
        assert str(denied_exc.value) == str(missing_exc.value).replace("does_not_exist", "cost")


class TestResolveMetric:
    def test_allowed_metric_resolves(self, scoped_service: CanonicService) -> None:
        binding = scoped_service.resolve_metric("revenue", principal=_VIEWER)
        assert binding.metric == "revenue"

    def test_denied_metric_raises_unresolved(self, scoped_service: CanonicService) -> None:
        with pytest.raises(Unresolved):
            scoped_service.resolve_metric("cost", principal=_VIEWER)

    def test_analyst_can_resolve_denied_metric(self, scoped_service: CanonicService) -> None:
        binding = scoped_service.resolve_metric("cost", principal=_ANALYST)
        assert binding.metric == "cost"
