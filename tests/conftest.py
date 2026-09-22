"""Root-level fixtures shared across all test suites."""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import litellm
import pytest
from fastmcp import Client, FastMCP

from canonic.config import CanonicConfig
from canonic.contracts.models import (
    AppliesTo,
    CanonicalRef,
    Guardrail,
    GuardrailKind,
    MetricBinding,
    Severity,
    Status,
)
from canonic.contracts.resolver import ContractResolver
from canonic.core.service import CanonicService
from canonic.semantic.models import Column, Dimension, Measure, SemanticSource

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path


def _response(content: str) -> SimpleNamespace:
    """Minimal litellm ModelResponse stand-in exposing the content path used by the drafter."""
    message = SimpleNamespace(content=content)
    usage = SimpleNamespace(prompt_tokens=10, completion_tokens=5, total_tokens=15)
    return SimpleNamespace(choices=[SimpleNamespace(message=message)], usage=usage)


@pytest.fixture
def fake_litellm(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Patch ``litellm.acompletion`` with a recording fake.

    Returns the captured-kwargs dict. The canned content defaults to a valid grain payload;
    a test can override behaviour with :func:`set_fake`.
    """
    captured: dict[str, Any] = {}
    state: dict[str, Any] = {"content": '{"grain": ["id"]}', "raises": None, "raises_times": 0}
    calls: list[dict[str, Any]] = []

    async def fake_acompletion(**kwargs: Any) -> SimpleNamespace:
        captured.update(kwargs)
        calls.append(dict(kwargs))
        if state["raises"] is not None and (
            state["raises_times"] == 0 or len(calls) <= state["raises_times"]
        ):
            raise state["raises"]
        return _response(state["content"])

    monkeypatch.setattr(litellm, "acompletion", fake_acompletion)
    captured["_state"] = state
    captured["_calls"] = calls
    return captured


@pytest.fixture
def set_fake(fake_litellm: dict[str, Any]) -> Callable[..., None]:
    """Helper to set the fake's canned content or raised exception."""

    def _set(
        *,
        content: str | None = None,
        raises: BaseException | None = None,
        raises_times: int = 0,
    ) -> None:
        if content is not None:
            fake_litellm["_state"]["content"] = content
        fake_litellm["_state"]["raises"] = raises
        fake_litellm["_state"]["raises_times"] = raises_times

    return _set


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
            Column(name="region", type="string", nullable=False),
            Column(name="channel", type="string", nullable=False),
            Column(name="created_at", type="timestamp", nullable=False),
        ],
        measures=[
            Measure(name="total_revenue", expr="sum(amount)", additivity="additive"),
            Measure(name="order_count", expr="count(order_id)", additivity="additive"),
        ],
        dimensions=[
            Dimension(name="order_date", column="created_at"),
            Dimension(name="status", column="status", label="Bestellstatus"),
            Dimension(name="region", column="region"),
            Dimension(name="channel", column="channel"),
        ],
    )


@pytest.fixture
def canonic_service(
    orders_source: SemanticSource, monkeypatch: pytest.MonkeyPatch
) -> CanonicService:
    monkeypatch.setenv("PG_PASSWORD", "testpw")
    binding = MetricBinding(
        metric="revenue",
        canonical=CanonicalRef(source="orders", measure="total_revenue"),
        aliases=["rev"],
        status=Status.ACTIVE,
    )
    order_count_binding = MetricBinding(
        metric="order_count",
        canonical=CanonicalRef(source="orders", measure="order_count"),
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
    resolver = ContractResolver(bindings=[binding, order_count_binding], guardrails=[guardrail])
    config = CanonicConfig.model_validate(
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
    return CanonicService(config=config, resolver=resolver, sources=[orders_source])


#: The MCP protocol eras a FastMCP 4 daemon serves simultaneously
#: (AMENDMENT-fastmcp4-adoption §1.4). ``"sessionless"`` is what `mode="auto"` picks
#: against a FastMCP 4 server: no `initialize`, every request self-contained.
#: ``"handshake"`` pins the pre-2026-07-28 behavior current agent clients still use.
MCP_ERAS = ("handshake", "sessionless")


def mcp_client(server: FastMCP, era: str) -> Client:
    """A FastMCP ``Client`` for *server* pinned to one protocol era.

    Every tool must behave identically on both eras, so tests that assert on payloads
    parametrize over :data:`MCP_ERAS` rather than taking whichever era the client
    negotiates by default.
    """
    if era == "handshake":
        return Client(server, mode="legacy")
    if era == "sessionless":
        return Client(server, mode="2026-07-28")
    raise ValueError(f"unknown MCP era: {era!r}")


@pytest.fixture
def report_project(tmp_path: Path) -> Path:
    """A minimal DuckDB-backed project with one committed report — no live network DB needed.

    Distinct from ``canonic_service`` (root conftest): ``run_report`` executes its
    section's query for real, which needs an actual connection, not just a resolver
    and semantic sources in memory.
    """
    import duckdb

    db_path = tmp_path / "warehouse.duckdb"
    con = duckdb.connect(str(db_path))
    con.execute(
        "CREATE TABLE orders (order_id INTEGER, amount DECIMAL(12,2), status VARCHAR);"
        "INSERT INTO orders VALUES (1, 100.00, 'paid');"
    )
    con.close()

    (tmp_path / "canonic.yaml").write_text(
        "version: 1\n"
        "project:\n  name: test\n  default_connection: warehouse_duckdb\n"
        "connections:\n"
        f"  - id: warehouse_duckdb\n    type: duckdb\n    params: {{path: {db_path}}}\n"
        "llm:\n  provider: openai_compatible\n  base_url: http://localhost/v1\n  model: llama3\n"
    )
    sem = tmp_path / "semantics" / "warehouse_duckdb"
    sem.mkdir(parents=True)
    (sem / "orders.yaml").write_text(
        "name: orders\nconnection: warehouse_duckdb\ntable: orders\ngrain: [order_id]\n"
        "columns:\n  - {name: order_id, type: int, nullable: false}\n"
        "  - {name: amount, type: decimal, nullable: false}\n"
        "  - {name: status, type: string, nullable: false}\n"
        "measures:\n  - {name: total_revenue, expr: 'sum(amount)', additivity: additive}\n"
        "dimensions:\n  - {name: status, column: status}\n"
    )
    metrics = tmp_path / "contracts" / "metrics"
    metrics.mkdir(parents=True)
    (metrics / "revenue.yaml").write_text(
        "metric: revenue\ncanonical:\n  source: orders\n  measure: total_revenue\nstatus: active\n"
    )
    reports = tmp_path / "reports"
    reports.mkdir(parents=True)
    (reports / "customer_report.yaml").write_text(
        "id: customer_report\ntitle: Customer Report\nsections:\n"
        "  - title: Revenue by status\n    query: {metrics: [revenue], dimensions: [status]}\n"
    )
    return tmp_path
