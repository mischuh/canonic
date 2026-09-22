"""Dual-era serving — AMENDMENT-fastmcp4-adoption §1.4, S18.

A FastMCP 4 daemon serves the handshake era and the sessionless ``2026-07-28`` era
from the same server object with no configuration. Every tool must return the same
core payload on both, since the tools are request/response only and hold no session
state. These tests pin that: the transport era is not allowed to leak into a payload.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import pytest

from canonic.core.service import CanonicService
from canonic.mcp.server import build_server
from tests.conftest import MCP_ERAS, mcp_client

if TYPE_CHECKING:
    from pathlib import Path


def _canonical(payload: Any) -> str:
    """Byte-comparable rendering of a tool payload, key order normalised."""
    return json.dumps(payload, sort_keys=True, default=str)


async def _call(server: Any, era: str, tool: str, args: dict[str, Any]) -> Any:
    async with mcp_client(server, era) as client:
        result = await client.call_tool(tool, args)
    return result.data


@pytest.mark.release_gate
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tool", "args"),
    [
        ("list_metrics", {}),
        ("describe_metric", {"name": "revenue"}),
    ],
)
async def test_metadata_tools_identical_across_eras(
    canonic_service: CanonicService, tool: str, args: dict[str, Any]
) -> None:
    """S18 AC1: the metadata tools return byte-identical payloads on both eras."""
    mcp = build_server(canonic_service)

    payloads = {era: await _call(mcp, era, tool, args) for era in MCP_ERAS}

    assert _canonical(payloads["handshake"]) == _canonical(payloads["sessionless"])


@pytest.mark.release_gate
@pytest.mark.asyncio
async def test_query_identical_across_eras(report_project: Path) -> None:
    """S18 AC1: ``query`` returns byte-identical payloads on both eras.

    Uses the DuckDB-backed project rather than ``canonic_service``, since ``query``
    executes for real and an in-memory resolver has nothing to execute against.
    """
    service = CanonicService.from_project(report_project)
    mcp = build_server(service)
    args = {"query": {"metrics": ["revenue"], "dimensions": ["status"]}}

    payloads = {era: await _call(mcp, era, "query", args) for era in MCP_ERAS}

    assert payloads["sessionless"]["result"]["rows"] == [["paid", "100.00"]]
    assert _canonical(payloads["handshake"]) == _canonical(payloads["sessionless"])


@pytest.mark.release_gate
@pytest.mark.asyncio
async def test_run_sql_identical_across_eras(report_project: Path) -> None:
    """S18 AC2: ``run_sql`` returns byte-identical payloads on both eras.

    The e2e walking skeleton covers the same tool against Postgres, but only where
    Docker is available. This keeps the era dimension of ``run_sql`` on the default
    suite as well.
    """
    service = CanonicService.from_project(report_project)
    mcp = build_server(service)
    args = {"sql": "SELECT order_id, amount FROM orders ORDER BY order_id"}

    payloads = {era: await _call(mcp, era, "run_sql", args) for era in MCP_ERAS}

    assert payloads["sessionless"]["rows"], "expected at least one row"
    assert _canonical(payloads["handshake"]) == _canonical(payloads["sessionless"])


@pytest.mark.asyncio
async def test_sessionless_client_performs_no_handshake(
    canonic_service: CanonicService,
) -> None:
    """The two eras are actually distinct, so the parity assertions above mean something.

    Without this, a regression that made ``mcp_client`` hand back the same era twice
    would leave every parity test above passing while covering only one path.
    """
    mcp = build_server(canonic_service)

    async with mcp_client(mcp, "handshake") as client:
        assert client.initialize_result is not None
        assert client.initialize_result.protocol_version == "2025-11-25"

    async with mcp_client(mcp, "sessionless") as client:
        assert client.initialize_result is None
