"""Tests for MCP tool registration and error mapping (canon/mcp/server.py).

Tools are called in-memory via the FastMCP Client against the built server so no
transport is needed.
"""

from __future__ import annotations

import pytest
from fastmcp import Client

from canon.config import CanonConfig
from canon.contracts.models import CanonicalRef, MetricBinding, Status
from canon.contracts.resolver import ContractResolver
from canon.core.service import CanonService  # noqa: TC001
from canon.mcp.server import build_server
from canon.semantic.models import Column, Dimension, Join, Measure, Relationship, SemanticSource


def _ambiguous_service(monkeypatch: pytest.MonkeyPatch) -> CanonService:
    """Build a CanonService with two join paths from 'owner' to 'dim'."""
    monkeypatch.setenv("PG_PASSWORD", "testpw")
    owner = SemanticSource(
        name="owner",
        connection="db",
        table="t.owner",
        grain=["id"],
        columns=[Column(name="id", type="string"), Column(name="amount", type="decimal")],
        measures=[Measure(name="m", expr="sum(amount)", additivity="additive")],
        joins=[
            Join(to="hop_a", on="owner.id = hop_a.id", relationship=Relationship.MANY_TO_ONE),
            Join(to="hop_b", on="owner.id = hop_b.id", relationship=Relationship.MANY_TO_ONE),
        ],
    )
    hop_a = SemanticSource(
        name="hop_a",
        connection="db",
        table="t.hop_a",
        grain=["id"],
        columns=[Column(name="id", type="string")],
        joins=[Join(to="dim", on="hop_a.id = dim.id", relationship=Relationship.MANY_TO_ONE)],
    )
    hop_b = SemanticSource(
        name="hop_b",
        connection="db",
        table="t.hop_b",
        grain=["id"],
        columns=[Column(name="id", type="string")],
        joins=[Join(to="dim", on="hop_b.id = dim.id", relationship=Relationship.MANY_TO_ONE)],
    )
    dim = SemanticSource(
        name="dim",
        connection="db",
        table="t.dim",
        grain=["id"],
        columns=[Column(name="id", type="string"), Column(name="region", type="string")],
        dimensions=[Dimension(name="region", column="region")],
    )
    resolver = ContractResolver(
        bindings=[
            MetricBinding(
                metric="m",
                canonical=CanonicalRef(source="owner", measure="m"),
                status=Status.ACTIVE,
            )
        ],
        guardrails=[],
    )
    config = CanonConfig.model_validate(
        {
            "version": 1,
            "project": {"name": "test", "default_connection": "db"},
            "connections": [
                {
                    "id": "db",
                    "type": "postgres",
                    "params": {"host": "localhost", "port": 5432, "dbname": "testdb", "user": "u"},
                    "credentials_ref": "env:PG_PASSWORD",
                }
            ],
        }
    )
    return CanonService(config=config, resolver=resolver, sources=[owner, hop_a, hop_b, dim])


@pytest.mark.asyncio
async def test_list_metrics(canon_service: CanonService) -> None:
    mcp = build_server(canon_service)
    async with Client(mcp) as client:
        result = await client.call_tool("list_metrics", {})
    metrics = result.data
    assert isinstance(metrics, list)
    assert any(m["metric"] == "revenue" for m in metrics)


@pytest.mark.asyncio
async def test_describe_metric(canon_service: CanonService) -> None:
    mcp = build_server(canon_service)
    async with Client(mcp) as client:
        result = await client.call_tool("describe_metric", {"name": "revenue"})
    data = result.data
    assert data["metric"] == "revenue"
    assert "grain" in data
    assert "dimensions" in data


@pytest.mark.asyncio
async def test_describe_metric_unresolved_returns_error(canon_service: CanonService) -> None:
    mcp = build_server(canon_service)
    async with Client(mcp) as client:
        result = await client.call_tool("describe_metric", {"name": "mrr"})
    data = result.data
    assert data["code"] == "unresolved"
    assert "result" not in data


@pytest.mark.asyncio
async def test_resolve_metric(canon_service: CanonService) -> None:
    mcp = build_server(canon_service)
    async with Client(mcp) as client:
        result = await client.call_tool("resolve_metric", {"name": "rev"})
    data = result.data
    assert data["metric"] == "revenue"
    assert data["source"] == "orders"


@pytest.mark.asyncio
async def test_resolve_metric_unresolved(canon_service: CanonService) -> None:
    mcp = build_server(canon_service)
    async with Client(mcp) as client:
        result = await client.call_tool("resolve_metric", {"name": "unknown"})
    data = result.data
    assert data["code"] == "unresolved"


@pytest.mark.asyncio
async def test_compile_query(canon_service: CanonService) -> None:
    mcp = build_server(canon_service)
    async with Client(mcp) as client:
        result = await client.call_tool("compile_query", {"query": {"metrics": ["revenue"]}})
    data = result.data
    assert "sql" in data["compiled"]
    assert "SELECT" in data["compiled"]["sql"].upper()
    assert data["metadata"]["resolved"]["metrics"]["revenue"] == "orders.total_revenue"
    assert any(g["id"] == "revenue-excludes-refunds" for g in data["metadata"]["guardrails_fired"])
    assert data["metadata"]["contract_schema"] == "1.4"
    # S12: related block is always present
    assert "related" in data["metadata"]
    assert isinstance(data["metadata"]["related"]["unused_dimensions"], list)
    assert isinstance(data["metadata"]["related"]["sibling_metrics"], list)
    # order_count is a sibling on the same source
    sibling_names = [m["name"] for m in data["metadata"]["related"]["sibling_metrics"]]
    assert "order_count" in sibling_names
    assert "revenue" not in sibling_names


@pytest.mark.asyncio
async def test_compile_query_unresolved_metric(canon_service: CanonService) -> None:
    mcp = build_server(canon_service)
    async with Client(mcp) as client:
        result = await client.call_tool("compile_query", {"query": {"metrics": ["nonexistent"]}})
    data = result.data
    assert data["code"] == "unresolved"


@pytest.mark.asyncio
async def test_compile_query_ambiguous_join_path_returns_actionable_candidates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    svc = _ambiguous_service(monkeypatch)
    mcp = build_server(svc)
    async with Client(mcp) as client:
        result = await client.call_tool(
            "compile_query", {"query": {"metrics": ["m"], "dimensions": ["region"]}}
        )
    data = result.data
    assert data["code"] == "ambiguous_join_path"
    candidates = data["candidates"]
    assert len(candidates) == 2
    vias = {tuple(c["via"]) for c in candidates}
    assert vias == {("hop_a", "dim"), ("hop_b", "dim")}
    for c in candidates:
        assert "via" in c
        assert "route" in c
        assert "joins" in c
        assert c["route"].startswith("owner →")


@pytest.mark.asyncio
async def test_compile_query_via_resolves_ambiguous_join_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    svc = _ambiguous_service(monkeypatch)
    mcp = build_server(svc)
    async with Client(mcp) as client:
        result = await client.call_tool(
            "compile_query",
            {"query": {"metrics": ["m"], "dimensions": ["region"], "via": ["hop_a", "dim"]}},
        )
    data = result.data
    assert "compiled" in data
    assert "hop_a" in data["compiled"]["sql"]
    assert "hop_b" not in data["compiled"]["sql"]
