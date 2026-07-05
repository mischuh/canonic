"""Adapter parity — SPEC-P0 §5 item 3.

Verifies that the MCP tools and the direct service-level paths produce
byte-identical core payloads. This is a proxy for the full CLI↔MCP parity gate
(the query/run_sql tools require a live DB connection, so they are covered by
e2e tests).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from fastmcp import Client

from canonic.compiler.query import SemanticQuery
from canonic.contract import CONTRACT_SCHEMA
from canonic.core.models import CompileOutput
from canonic.mcp.server import build_server

if TYPE_CHECKING:
    from canonic.core.service import CanonicService


@pytest.mark.release_gate
@pytest.mark.asyncio
async def test_resolve_metric_parity(canonic_service: CanonicService) -> None:
    """MCP resolve_metric and direct service path return identical payloads."""
    mcp = build_server(canonic_service)

    async with Client(mcp) as client:
        result = await client.call_tool("resolve_metric", {"name": "revenue"})
    mcp_payload = result.data

    binding = canonic_service.resolve_metric("revenue")
    service_payload = {
        "metric": binding.metric,
        "source": binding.source,
        "measure": binding.measure,
    }

    assert mcp_payload == service_payload


@pytest.mark.release_gate
@pytest.mark.asyncio
async def test_compile_query_parity(canonic_service: CanonicService) -> None:
    """MCP compile_query and direct service path return identical payloads."""
    sq = SemanticQuery(metrics=["revenue"])
    mcp = build_server(canonic_service)

    async with Client(mcp) as client:
        result = await client.call_tool("compile_query", {"query": {"metrics": ["revenue"]}})
    mcp_payload = result.data

    compile_result = canonic_service.compile_query(sq)
    service_payload = CompileOutput.from_compile_result(compile_result).model_dump(mode="json")

    assert mcp_payload == service_payload


@pytest.mark.asyncio
async def test_get_overview_parity(canonic_service: CanonicService) -> None:
    """MCP get_overview and direct service path return identical payloads (AC5)."""
    mcp = build_server(canonic_service)

    async with Client(mcp) as client:
        result = await client.call_tool("get_overview", {})
    mcp_payload = result.data

    service_payload = canonic_service.get_overview().model_dump(mode="json")

    assert mcp_payload == service_payload


@pytest.mark.asyncio
async def test_describe_metric_parity(canonic_service: CanonicService) -> None:
    """MCP describe_metric and direct service path return identical payloads (AC5)."""
    mcp = build_server(canonic_service)

    async with Client(mcp) as client:
        result = await client.call_tool("describe_metric", {"name": "revenue"})
    mcp_payload = result.data

    service_payload = canonic_service.describe_metric("revenue").model_dump(mode="json")

    assert mcp_payload == service_payload


@pytest.mark.asyncio
async def test_contract_info_returns_schema(canonic_service: CanonicService) -> None:
    """contract_info tool returns the current contract_schema version."""
    mcp = build_server(canonic_service)
    async with Client(mcp) as client:
        result = await client.call_tool("contract_info", {})
    assert result.data == {"contract_schema": CONTRACT_SCHEMA}


@pytest.mark.asyncio
async def test_negotiate_contract_accepts_matching_major(canonic_service: CanonicService) -> None:
    mcp = build_server(canonic_service)
    async with Client(mcp) as client:
        result = await client.call_tool("negotiate_contract", {"contract_major": 2})
    assert result.data["accepted"] is True
    assert result.data["contract_schema"] == CONTRACT_SCHEMA


@pytest.mark.asyncio
async def test_negotiate_contract_rejects_mismatched_major(canonic_service: CanonicService) -> None:
    from fastmcp.exceptions import ToolError

    mcp = build_server(canonic_service)
    with pytest.raises(ToolError, match="MAJOR mismatch"):
        async with Client(mcp) as client:
            await client.call_tool("negotiate_contract", {"contract_major": 99})
