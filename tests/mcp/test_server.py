"""Tests for MCP tool registration and error mapping (canon/mcp/server.py).

Tools are called in-memory via the FastMCP Client against the built server so no
transport is needed.
"""

from __future__ import annotations

import pytest
from fastmcp import Client

from canon.core.service import CanonService  # noqa: TC001
from canon.mcp.server import build_server


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
    assert data["metadata"]["contract_schema"] == "1.2"


@pytest.mark.asyncio
async def test_compile_query_unresolved_metric(canon_service: CanonService) -> None:
    mcp = build_server(canon_service)
    async with Client(mcp) as client:
        result = await client.call_tool("compile_query", {"query": {"metrics": ["nonexistent"]}})
    data = result.data
    assert data["code"] == "unresolved"
