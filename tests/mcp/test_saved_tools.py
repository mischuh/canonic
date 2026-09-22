"""Tests for the six saved-query/personal-report MCP tools
(AMENDMENT-user-scoped-queries-reports, S19-S23, S25).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import duckdb
import pytest
from fastmcp import Client

from canonic.config import CanonicConfig
from canonic.contracts.models import CanonicalRef, MetricBinding, RoleDef, RolePolicy, Status
from canonic.contracts.principal import Principal
from canonic.contracts.resolver import ContractResolver
from canonic.core.service import CanonicService
from canonic.mcp.server import build_server
from canonic.semantic.models import Column, Dimension, Measure, SemanticSource

if TYPE_CHECKING:
    from pathlib import Path

_SEED_SQL = """
CREATE TABLE orders (order_id INTEGER PRIMARY KEY, amount DECIMAL(12,2), status VARCHAR);
INSERT INTO orders VALUES (1, 100.00, 'paid');
"""


@pytest.fixture
def project(tmp_path: Path) -> CanonicService:
    db_path = tmp_path / "warehouse.duckdb"
    con = duckdb.connect(str(db_path))
    con.execute(_SEED_SQL)
    con.close()

    orders_source = SemanticSource(
        name="orders",
        connection="warehouse_duckdb",
        table="orders",
        grain=["order_id"],
        columns=[
            Column(name="order_id", type="string", nullable=False),
            Column(name="amount", type="decimal", nullable=False),
            Column(name="status", type="string", nullable=False),
        ],
        measures=[Measure(name="total_revenue", expr="sum(amount)", additivity="additive")],
        dimensions=[Dimension(name="status", column="status")],
    )
    revenue_binding = MetricBinding(
        metric="revenue",
        canonical=CanonicalRef(source="orders", measure="total_revenue"),
        status=Status.ACTIVE,
    )
    resolver = ContractResolver(bindings=[revenue_binding], guardrails=[])
    config = CanonicConfig.model_validate(
        {
            "version": 1,
            "project": {"name": "test", "default_connection": "warehouse_duckdb"},
            "connections": [
                {"id": "warehouse_duckdb", "type": "duckdb", "params": {"path": str(db_path)}}
            ],
            "llm": {
                "provider": "openai_compatible",
                "base_url": "http://localhost/v1",
                "model": "llama3",
            },
        }
    )
    return CanonicService(
        config=config, resolver=resolver, sources=[orders_source], project_root=tmp_path
    )


@pytest.fixture
def roled_project(tmp_path: Path) -> CanonicService:
    """Same project, but with a role policy: 'viewer' denies manage_saved_content."""
    db_path = tmp_path / "warehouse.duckdb"
    con = duckdb.connect(str(db_path))
    con.execute(_SEED_SQL)
    con.close()

    orders_source = SemanticSource(
        name="orders",
        connection="warehouse_duckdb",
        table="orders",
        grain=["order_id"],
        columns=[
            Column(name="order_id", type="string", nullable=False),
            Column(name="amount", type="decimal", nullable=False),
            Column(name="status", type="string", nullable=False),
        ],
        measures=[Measure(name="total_revenue", expr="sum(amount)", additivity="additive")],
        dimensions=[Dimension(name="status", column="status")],
    )
    revenue_binding = MetricBinding(
        metric="revenue",
        canonical=CanonicalRef(source="orders", measure="total_revenue"),
        status=Status.ACTIVE,
    )
    roles = RolePolicy(
        schema="roles/v1",
        claim="roles",
        roles={"viewer": RoleDef(metrics={"allow": ["*"]}, manage_saved_content=False)},
    )
    resolver = ContractResolver(bindings=[revenue_binding], guardrails=[], roles=roles)
    config = CanonicConfig.model_validate(
        {
            "version": 1,
            "project": {"name": "test", "default_connection": "warehouse_duckdb"},
            "connections": [
                {"id": "warehouse_duckdb", "type": "duckdb", "params": {"path": str(db_path)}}
            ],
            "llm": {
                "provider": "openai_compatible",
                "base_url": "http://localhost/v1",
                "model": "llama3",
            },
        }
    )
    return CanonicService(
        config=config, resolver=resolver, sources=[orders_source], project_root=tmp_path
    )


@pytest.mark.asyncio
async def test_save_list_delete_query_round_trip(project: CanonicService) -> None:
    mcp = build_server(project)
    async with Client(mcp) as client:
        saved = await client.call_tool(
            "save_query",
            {
                "query": {"metrics": ["revenue"]},
                "title": "Revenue",
                "question": "What is revenue?",
                "user": "alice",
            },
        )
        query_id = saved.data["id"]

        listed = await client.call_tool("list_saved_queries", {"user": "alice"})
        assert [q["id"] for q in listed.data["queries"]] == [query_id]

        listed_bob = await client.call_tool("list_saved_queries", {"user": "bob"})
        assert listed_bob.data["queries"] == []

        await client.call_tool("delete_query", {"query_id": query_id, "user": "alice"})
        listed_after = await client.call_tool("list_saved_queries", {"user": "alice"})
        assert listed_after.data["queries"] == []


@pytest.mark.asyncio
async def test_compose_run_update_delete_report_round_trip(project: CanonicService) -> None:
    mcp = build_server(project)
    async with Client(mcp) as client:
        saved = await client.call_tool(
            "save_query", {"query": {"metrics": ["revenue"]}, "user": "alice"}
        )
        query_id = saved.data["id"]

        composed = await client.call_tool(
            "compose_report", {"title": "Alice Q1", "query_ids": [query_id], "user": "alice"}
        )
        report_id = composed.data["id"]
        assert composed.data["scope"] == "user:alice"

        run = await client.call_tool("run_report", {"report_id": report_id, "user": "alice"})
        assert len(run.data["sections"]) == 1
        assert run.data["sections"][0]["error"] is None
        assert run.data["sections"][0]["id"] == query_id

        described = await client.call_tool(
            "describe_report", {"report_id": report_id, "user": "alice"}
        )
        assert described.data["report_id"] == report_id
        assert [s["id"] for s in described.data["sections"]] == [query_id]

        overview = await client.call_tool("get_overview", {"user": "alice"})
        assert any(r["id"] == report_id for r in overview.data["reports"])

        listed = await client.call_tool("list_reports", {"user": "alice"})
        assert [r["id"] for r in listed.data["reports"]] == [report_id]

        # Removing the only section would leave an empty report — rejected, surfaced as a
        # structured tool error rather than an exception (canonic_error_response).
        rejected = await client.call_tool(
            "update_report",
            {"report_id": report_id, "remove_sections": [query_id], "user": "alice"},
        )
        assert "code" in rejected.data

        await client.call_tool("delete_report", {"report_id": report_id, "user": "alice"})
        listed_after = await client.call_tool("list_reports", {"user": "alice"})
        assert listed_after.data["reports"] == []

        # Deleting the report never deletes the source query (S23 AC1).
        remaining = await client.call_tool("list_saved_queries", {"user": "alice"})
        assert [q["id"] for q in remaining.data["queries"]] == [query_id]


@pytest.mark.asyncio
async def test_bob_cannot_run_or_delete_alices_report(project: CanonicService) -> None:
    mcp = build_server(project)
    async with Client(mcp) as client:
        saved = await client.call_tool(
            "save_query", {"query": {"metrics": ["revenue"]}, "user": "alice"}
        )
        composed = await client.call_tool(
            "compose_report", {"title": "R", "query_ids": [saved.data["id"]], "user": "alice"}
        )
        report_id = composed.data["id"]

        run = await client.call_tool("run_report", {"report_id": report_id, "user": "bob"})
        assert run.data["code"] == "unresolved"

        deleted = await client.call_tool("delete_report", {"report_id": report_id, "user": "bob"})
        assert deleted.data["code"] == "unresolved"


@pytest.mark.asyncio
async def test_viewer_role_denied_save_and_list(roled_project: CanonicService) -> None:
    """A role with manage_saved_content: false is refused, surfaced as tenant_forbidden."""
    mcp = build_server(roled_project, session_principal=Principal(tenant=None, roles=("viewer",)))
    async with Client(mcp) as client:
        rejected = await client.call_tool(
            "save_query", {"query": {"metrics": ["revenue"]}, "user": "alice"}
        )
        assert rejected.data["code"] == "tenant_forbidden"

        listed = await client.call_tool("list_saved_queries", {"user": "alice"})
        assert listed.data["code"] == "tenant_forbidden"

        composed = await client.call_tool(
            "compose_report", {"title": "R", "query_ids": ["whatever"], "user": "alice"}
        )
        assert composed.data["code"] == "tenant_forbidden"
