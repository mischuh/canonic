"""Fixtures for MCP adapter tests."""

from __future__ import annotations

import pytest

from canon.config import CanonConfig
from canon.contracts.models import (
    AppliesTo,
    CanonicalRef,
    Guardrail,
    GuardrailKind,
    MetricBinding,
    Severity,
    Status,
)
from canon.contracts.resolver import ContractResolver
from canon.core.service import CanonService
from canon.semantic.models import Column, Dimension, Measure, SemanticSource


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
            Column(name="created_at", type="timestamp", nullable=False),
        ],
        measures=[Measure(name="total_revenue", expr="sum(amount)", additivity="additive")],
        dimensions=[
            Dimension(name="order_date", column="created_at"),
            Dimension(name="status", column="status"),
        ],
    )


@pytest.fixture
def canon_service(orders_source: SemanticSource, monkeypatch: pytest.MonkeyPatch) -> CanonService:
    monkeypatch.setenv("PG_PASSWORD", "testpw")
    binding = MetricBinding(
        metric="revenue",
        canonical=CanonicalRef(source="orders", measure="total_revenue"),
        aliases=["rev"],
        status=Status.ACTIVE,
    )
    guardrail = Guardrail(
        id="revenue-excludes-refunds",
        applies_to=AppliesTo(source="orders", measure="total_revenue"),
        kind=GuardrailKind.MANDATORY_FILTER,
        filter="status != 'refunded'",
        severity=Severity.ERROR,
        rationale="Refunds are reversals, not revenue.",
    )
    resolver = ContractResolver(bindings=[binding], guardrails=[guardrail])
    config = CanonConfig.model_validate(
        {
            "version": 1,
            "project": {"name": "test", "default_connection": "warehouse_pg"},
            "connections": [
                {
                    "id": "warehouse_pg",
                    "type": "postgres",
                    "params": {
                        "host": "localhost",
                        "port": 5432,
                        "dbname": "testdb",
                        "user": "test",
                    },
                    "credentials_ref": "env:PG_PASSWORD",
                }
            ],
            "llm": {
                "provider": "openai_compatible",
                "base_url": "http://localhost/v1",
                "model": "llama3",
            },
        }
    )
    return CanonService(config=config, resolver=resolver, sources=[orders_source])
