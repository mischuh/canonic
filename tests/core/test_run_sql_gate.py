"""``run_sql`` fail-closed gate (SPEC-E12 §6): role denial and the layer-2 RLS attestation.

Both checks happen before any connector is opened — no live database needed here.
"""

from __future__ import annotations

import pytest

from canonic.config import CanonicConfig
from canonic.contracts.models import (
    OnMissingPrincipal,
    RoleDef,
    RolePolicy,
    ScopedSource,
    TenancyPolicy,
    UndeclaredSource,
)
from canonic.contracts.principal import Principal
from canonic.contracts.resolver import ContractResolver
from canonic.core.service import CanonicService
from canonic.exc import TenantForbidden
from canonic.semantic.models import Column, Measure, SemanticSource

_ANALYST_ROLE = "platform_analyst"
_VIEWER_ROLE = "merchant_viewer"
_ADMIN_ROLE = "merchant_admin"


def _config(*, rls_enforced: bool) -> dict:
    return {
        "version": 1,
        "project": {"name": "test", "default_connection": "warehouse_pg"},
        "connections": [
            {
                "id": "warehouse_pg",
                "type": "postgres",
                "params": {"host": "localhost", "port": 5432, "dbname": "testdb", "user": "test"},
                "credentials_ref": "env:PG_PASSWORD",
                "rls_enforced": rls_enforced,
            }
        ],
        "llm": {
            "provider": "openai_compatible",
            "base_url": "http://localhost/v1",
            "model": "llama3",
        },
    }


@pytest.fixture
def orders_source() -> SemanticSource:
    return SemanticSource(
        name="orders",
        connection="warehouse_pg",
        table="fct_orders",
        grain=["order_id"],
        columns=[
            Column(name="order_id", type="string", nullable=False),
            Column(name="amount", type="decimal", nullable=False),
            Column(name="merchant_id", type="string", nullable=False),
        ],
        measures=[Measure(name="total_revenue", expr="sum(amount)", additivity="additive")],
    )


@pytest.fixture
def role_policy() -> RolePolicy:
    return RolePolicy(
        schema_="roles/v1",
        claim="roles",
        roles={
            _VIEWER_ROLE: RoleDef(run_sql=False),
            # run_sql: true but NOT tenancy_exempt — the ordinary "role permits raw SQL"
            # case, distinct from the platform-operator escape hatch below.
            _ADMIN_ROLE: RoleDef(run_sql=True, tenancy_exempt=False),
            _ANALYST_ROLE: RoleDef(run_sql=True, tenancy_exempt=True),
        },
    )


@pytest.fixture
def tenancy_policy() -> TenancyPolicy:
    return TenancyPolicy(
        schema_="tenancy/v1",
        claim="merchant_id",
        on_missing_principal=OnMissingPrincipal.DENY,
        scoped_sources=[ScopedSource(source="orders", column="merchant_id")],
        shared_sources=[],
        undeclared_source=UndeclaredSource.DENY,
    )


def _service(
    orders_source: SemanticSource,
    role_policy: RolePolicy,
    tenancy_policy: TenancyPolicy | None,
    monkeypatch: pytest.MonkeyPatch,
    *,
    rls_enforced: bool,
) -> CanonicService:
    monkeypatch.setenv("PG_PASSWORD", "testpassword")
    resolver = ContractResolver(
        bindings=[], guardrails=[], tenancy=tenancy_policy, roles=role_policy
    )
    config = CanonicConfig.model_validate(_config(rls_enforced=rls_enforced))
    return CanonicService(config=config, resolver=resolver, sources=[orders_source])


@pytest.fixture
def _stub_connector(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub ``ServiceContext.connector_for`` so a gate-passing ``run_sql`` never opens a
    real connection — only the gate itself (raised before this is even called) is under
    test in the "forbidden" cases; the "allowed" cases just need something to run against.
    """
    from canonic.connectors.base import Capability, ResultSet
    from canonic.core.context import ServiceContext

    class _StubConnector:
        def capabilities(self) -> list[Capability]:
            return [Capability.RUN_READ_ONLY_SQL]

        async def run_read_only_sql(self, sql: str) -> ResultSet:
            return ResultSet(columns=[], rows=[], truncated=False, bytes_scanned=None)

        async def aclose(self) -> None:
            return None

    monkeypatch.setattr(ServiceContext, "connector_for", lambda self, connection: _StubConnector())


class TestRoleDeniesRunSql:
    async def test_run_sql_false_role_is_forbidden(
        self, orders_source, role_policy, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        service = _service(
            orders_source,
            role_policy,
            tenancy_policy=None,
            monkeypatch=monkeypatch,
            rls_enforced=False,
        )
        with pytest.raises(TenantForbidden, match="run_sql: false"):
            await service.run_sql(
                "SELECT 1", principal=Principal(tenant=None, roles=(_VIEWER_ROLE,))
            )

    @pytest.mark.usefixtures("_stub_connector")
    async def test_run_sql_true_role_without_tenancy_is_allowed(
        self, orders_source, role_policy, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No tenancy policy configured — the rls_enforced connection gate never applies,
        regardless of the connection's own attestation."""
        service = _service(
            orders_source,
            role_policy,
            tenancy_policy=None,
            monkeypatch=monkeypatch,
            rls_enforced=False,
        )
        await service.run_sql("SELECT 1", principal=Principal(tenant=None, roles=(_ANALYST_ROLE,)))


class TestTenancyRlsGate:
    async def test_refused_without_rls_enforced_attestation(
        self, orders_source, role_policy, tenancy_policy, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        service = _service(
            orders_source,
            role_policy,
            tenancy_policy=tenancy_policy,
            monkeypatch=monkeypatch,
            rls_enforced=False,
        )
        with pytest.raises(TenantForbidden, match="rls_enforced"):
            await service.run_sql(
                "SELECT 1", principal=Principal(tenant="4711", roles=(_ADMIN_ROLE,))
            )

    @pytest.mark.usefixtures("_stub_connector")
    async def test_allowed_with_rls_enforced_and_non_exempt_role(
        self, orders_source, role_policy, tenancy_policy, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        service = _service(
            orders_source,
            role_policy,
            tenancy_policy=tenancy_policy,
            monkeypatch=monkeypatch,
            rls_enforced=True,
        )
        await service.run_sql("SELECT 1", principal=Principal(tenant="4711", roles=(_ADMIN_ROLE,)))

    @pytest.mark.usefixtures("_stub_connector")
    async def test_tenancy_exempt_role_skips_rls_gate_even_when_unattested(
        self, orders_source, role_policy, tenancy_policy, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A tenancy_exempt principal (the platform-operator escape hatch) never needs the
        connection to carry rls_enforced: true — it isn't reading anyone else's rows through
        an unfiltered per-tenant boundary, it's the one caller sanctioned to read across
        tenants at all (SPEC-E12 §6)."""
        service = _service(
            orders_source,
            role_policy,
            tenancy_policy=tenancy_policy,
            monkeypatch=monkeypatch,
            rls_enforced=False,
        )
        await service.run_sql("SELECT 1", principal=Principal(tenant=None, roles=(_ANALYST_ROLE,)))
