"""Adapter parity — SPEC-P0 §5 item 3.

Verifies that the MCP ``compile_query`` tool and the direct service-level
compile path produce byte-identical core payloads. This is a proxy for the
full CLI↔MCP parity gate (the query/run_sql tools require a live DB connection,
so they are covered by e2e tests).
"""

from __future__ import annotations

import pytest
from fastmcp import Client

from canon.compiler.query import SemanticQuery
from canon.core.models import CompileOutput
from canon.core.service import CanonService
from canon.mcp.server import build_server


@pytest.mark.asyncio
async def test_compile_query_parity(canon_service: CanonService) -> None:
    """MCP compile_query and direct service path return identical payloads."""
    sq = SemanticQuery(metrics=["revenue"])
    mcp = build_server(canon_service)

    async with Client(mcp) as client:
        result = await client.call_tool("compile_query", {"query": {"metrics": ["revenue"]}})
    mcp_payload = result.data

    compile_result = canon_service.compile_query(sq)
    service_payload = CompileOutput.from_compile_result(compile_result).model_dump(mode="json")

    assert mcp_payload == service_payload


@pytest.mark.asyncio
async def test_contract_info_returns_schema(canon_service: CanonService) -> None:
    """contract_info tool returns the current contract_schema version."""
    from canon.contract import CONTRACT_SCHEMA

    mcp = build_server(canon_service)
    async with Client(mcp) as client:
        result = await client.call_tool("contract_info", {})
    assert result.data == {"contract_schema": CONTRACT_SCHEMA}


@pytest.mark.asyncio
async def test_negotiate_contract_accepts_matching_major(canon_service: CanonService) -> None:
    mcp = build_server(canon_service)
    async with Client(mcp) as client:
        result = await client.call_tool("negotiate_contract", {"contract_major": 1})
    assert result.data["accepted"] is True
    assert result.data["contract_schema"] == "1.0"


@pytest.mark.asyncio
async def test_negotiate_contract_rejects_mismatched_major(canon_service: CanonService) -> None:
    from fastmcp.exceptions import ToolError

    mcp = build_server(canon_service)
    with pytest.raises(ToolError, match="MAJOR mismatch"):
        async with Client(mcp) as client:
            await client.call_tool("negotiate_contract", {"contract_major": 99})
