"""Canonic's own internal correctness checks run tenant-exempt (SPEC-E12 §7).

Assertions (the CI/harness oracle) and static report validation both check whether
something *compiles*, independent of any caller identity — not "who is asking". Under a
tenancy policy with ``on_missing_principal: deny`` (the fail-closed default), a bare
``compile(principal=None)`` would raise ``TenantUnresolved`` for any query touching a
scoped source; without the ``SYSTEM_PRINCIPAL`` exemption these two internal paths would
break for every project that adopts tenancy, regardless of who is actually being served.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

import canonic.core.context as context_mod
from canonic.config import CanonicConfig
from canonic.connectors.base import Capability, ConnectorBase, Health, ResultColumn, ResultSet
from canonic.contracts.models import Assertion as AssertionContract
from canonic.contracts.models import (
    AssertionExpect,
    CanonicalRef,
    MetricBinding,
    OnMissingPrincipal,
    ScopedSource,
    TenancyPolicy,
    UndeclaredSource,
)
from canonic.contracts.resolver import ContractResolver
from canonic.core.service import CanonicService
from canonic.semantic.models import Column, Measure, SemanticSource

if TYPE_CHECKING:
    from pathlib import Path


class _FakeConnector(ConnectorBase):
    """A read-only connector that returns a canned result for every query."""

    def __init__(self, result: ResultSet) -> None:
        self._result = result

    def capabilities(self) -> list[Capability]:
        return [Capability.RUN_READ_ONLY_SQL]

    async def test_connection(self) -> Health:  # pragma: no cover — unused
        return Health(status="ok")

    async def run_read_only_sql(self, sql: str) -> ResultSet:
        return self._result

    async def aclose(self) -> None:
        return None


def _config() -> CanonicConfig:
    return CanonicConfig.model_validate(
        {
            "version": 1,
            "project": {"name": "test", "default_connection": "warehouse_pg"},
            "connections": [
                {
                    "id": "warehouse_pg",
                    "type": "postgres",
                    "params": {"host": "h", "port": 5432, "dbname": "d", "user": "u"},
                    "credentials_ref": "env:PG_PASSWORD",
                }
            ],
            "llm": {"provider": "openai_compatible", "base_url": "http://x/v1", "model": "m"},
        }
    )


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
def deny_by_default_tenancy() -> TenancyPolicy:
    """A tenancy policy that raises ``TenantUnresolved`` for any query with no principal —
    the fail-closed default a real project would ship with."""
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
    tenancy: TenancyPolicy,
    monkeypatch: pytest.MonkeyPatch,
    *,
    assertions: list[AssertionContract] = (),  # type: ignore[assignment]
    project_root: Path | None = None,
) -> CanonicService:
    monkeypatch.setenv("PG_PASSWORD", "pw")
    resolver = ContractResolver(
        bindings=[
            MetricBinding(
                metric="revenue",
                canonical=CanonicalRef(source="orders", measure="total_revenue"),
            )
        ],
        guardrails=[],
        assertions=list(assertions),
        tenancy=tenancy,
    )
    return CanonicService(
        config=_config(), resolver=resolver, sources=[orders_source], project_root=project_root
    )


async def test_assertion_runs_exempt_under_deny_by_default_tenancy(
    orders_source: SemanticSource,
    deny_by_default_tenancy: TenancyPolicy,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = ResultSet(columns=[ResultColumn(name="total_revenue", type="decimal")], rows=[[100]])
    monkeypatch.setattr(
        context_mod.default_factory, "for_id", lambda *a, **k: _FakeConnector(result)
    )
    assertion = AssertionContract(
        id="revenue-check",
        query={"metrics": ["revenue"]},
        expect=AssertionExpect(rows=1, values={"total_revenue": 100}),
        source_of_truth="Finance close",
    )
    service = _service(orders_source, deny_by_default_tenancy, monkeypatch, assertions=[assertion])

    # No principal is ever supplied — a caller-facing query() call under this policy would
    # raise TenantUnresolved; the assertion harness must not.
    outcomes = await service.check_assertions()
    assert len(outcomes) == 1
    assert outcomes[0].passed is True


async def test_run_accuracy_harness_runs_exempt_under_deny_by_default_tenancy(
    orders_source: SemanticSource,
    deny_by_default_tenancy: TenancyPolicy,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = ResultSet(columns=[ResultColumn(name="total_revenue", type="decimal")], rows=[[100]])
    monkeypatch.setattr(
        context_mod.default_factory, "for_id", lambda *a, **k: _FakeConnector(result)
    )
    assertion = AssertionContract(
        id="revenue-check",
        query={"metrics": ["revenue"]},
        expect=AssertionExpect(rows=1, values={"total_revenue": 100}),
        source_of_truth="Finance close",
    )
    service = _service(orders_source, deny_by_default_tenancy, monkeypatch, assertions=[assertion])

    report = await service.run_accuracy_harness()
    assert report.accuracy == 1.0


def test_validate_reports_runs_exempt_under_deny_by_default_tenancy(
    orders_source: SemanticSource,
    deny_by_default_tenancy: TenancyPolicy,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reports_dir = tmp_path / "reports"
    reports_dir.mkdir()
    (reports_dir / "revenue_report.yaml").write_text(
        "id: revenue_report\ntitle: Revenue Report\nsections:\n"
        "  - title: Revenue\n    query: {metrics: [revenue]}\n"
    )
    service = _service(orders_source, deny_by_default_tenancy, monkeypatch, project_root=tmp_path)

    # Must not raise TenantUnresolved even though no principal is ever supplied here.
    service.validate_reports()
