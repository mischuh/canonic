"""Fixtures for core-service tests — a minimal in-memory project."""

from __future__ import annotations

from pathlib import Path  # noqa: TC003

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
from canon.semantic.models import (
    Column,
    Dimension,
    Measure,
    SemanticSource,
)

_CONFIG_YAML = """\
version: 1
project:
  name: test-project
  default_connection: warehouse_pg
connections:
  - id: warehouse_pg
    type: postgres
    params:
      host: localhost
      port: 5432
      dbname: testdb
      user: test
    credentials_ref: env:PG_PASSWORD
llm:
  provider: openai_compatible
  base_url: http://localhost:11434/v1
  model: llama3
"""

_ORDERS_YAML = """\
name: orders
connection: warehouse_pg
table: analytics.fct_orders
grain: [order_id]
columns:
  - { name: order_id, type: string, nullable: false }
  - { name: amount,   type: decimal, nullable: false }
  - { name: status,   type: string, nullable: false }
  - { name: created_at, type: timestamp, nullable: false }
measures:
  - name: total_revenue
    expr: "sum(amount)"
    additivity: additive
dimensions:
  - { name: order_date, column: created_at }
  - { name: status, column: status }
"""

_BINDING_YAML = """\
metric: revenue
canonical:
  source: orders
  measure: total_revenue
aliases: ["rev", "net revenue"]
status: active
"""

_GUARDRAIL_YAML = """\
id: revenue-excludes-refunds
applies_to:
  source: orders
  measure: total_revenue
kind: mandatory_filter
filter: "status != 'refunded'"
severity: error
rationale: "Refunds are reversals, not revenue."
"""


@pytest.fixture
def project_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A minimal project root with config, semantics, and contracts."""
    (tmp_path / "canon.yaml").write_text(_CONFIG_YAML)

    sem = tmp_path / "semantics" / "warehouse_pg"
    sem.mkdir(parents=True)
    (sem / "orders.yaml").write_text(_ORDERS_YAML)

    (tmp_path / "contracts" / "metrics").mkdir(parents=True)
    (tmp_path / "contracts" / "metrics" / "revenue.yaml").write_text(_BINDING_YAML)

    (tmp_path / "contracts" / "guardrails").mkdir(parents=True)
    (tmp_path / "contracts" / "guardrails" / "revenue-excludes-refunds.yaml").write_text(
        _GUARDRAIL_YAML
    )

    monkeypatch.chdir(tmp_path)
    return tmp_path


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
def revenue_binding() -> MetricBinding:
    return MetricBinding(
        metric="revenue",
        canonical=CanonicalRef(source="orders", measure="total_revenue"),
        aliases=["rev", "net revenue"],
        status=Status.ACTIVE,
    )


@pytest.fixture
def ambiguous_binding(revenue_binding: MetricBinding) -> MetricBinding:
    return MetricBinding(
        metric="revenue",
        canonical=CanonicalRef(source="orders", measure="total_revenue"),
        aliases=[],
        status=Status.ACTIVE,
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
def canon_service(
    revenue_binding: MetricBinding,
    refund_guardrail: Guardrail,
    orders_source: SemanticSource,
    monkeypatch: pytest.MonkeyPatch,
) -> CanonService:
    """A CanonService wired from in-memory objects (no filesystem needed)."""
    monkeypatch.setenv("PG_PASSWORD", "testpassword")
    resolver = ContractResolver(
        bindings=[revenue_binding],
        guardrails=[refund_guardrail],
    )
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
