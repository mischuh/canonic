"""``app.getcanonic/contract`` MCP server extension — AMENDMENT-fastmcp4-adoption §2, S19."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from unittest.mock import Mock

import pytest
from fastmcp import Client
from mcp.client.extension import ClientExtension
from mcp.shared.exceptions import MCPError
from mcp.types import INVALID_PARAMS

from canonic.contract import CONTRACT_SCHEMA
from canonic.mcp.extensions import ContractExtension
from canonic.mcp.server import build_server

if TYPE_CHECKING:
    from canonic.core.service import CanonicService

_IDENTIFIER = ContractExtension.identifier


class _DeclaringClientExtension(ClientExtension):
    """A client extension that declares a fixed contract_schema for every request."""

    identifier = _IDENTIFIER

    def __init__(self, major: int, minor: int = 0) -> None:
        self._settings = {"major": major, "minor": minor}

    def settings(self) -> dict[str, Any]:
        return self._settings


@pytest.mark.asyncio
async def test_contract_extension_advertised_in_discover(canonic_service: CanonicService) -> None:
    """S19 AC1: the settings dict appears under capabilities.extensions on server/discover.

    ``mode="auto"`` is required here, not the ``mcp_client(server, "sessionless")`` helper:
    that helper pins the protocol version directly (``mode="2026-07-28"``), which skips the
    wire round trip and synthesizes a minimal, capability-free ``DiscoverResult`` — fine for
    tool-call parity tests, but it never exercises the real ``server/discover`` response this
    test needs. ``mode="auto"`` probes ``server/discover`` for real.

    The handshake era cannot show this at all: the MCP SDK's pre-2026 version sieve strips
    ``capabilities.extensions`` from the legacy ``initialize`` response entirely (upstream
    sdk-feedback #2). Per-request enforcement (the other half of S19) is independent of that
    and is covered below without pinning an era.
    """
    mcp = build_server(canonic_service)
    async with Client(mcp, mode="auto") as client:
        capabilities = client.server_capabilities
        assert capabilities is not None
        assert capabilities.extensions is not None
        assert capabilities.extensions[_IDENTIFIER] == ContractExtension().settings()


@pytest.mark.asyncio
async def test_major_mismatch_rejected_before_tool_runs(canonic_service: CanonicService) -> None:
    """S19 AC2: a declared MAJOR mismatch fails the call with INVALID_PARAMS, no tool execution."""
    mcp = build_server(canonic_service)
    spy = Mock(wraps=canonic_service.list_metrics)
    canonic_service.list_metrics = spy  # type: ignore[method-assign]

    server_major = int(CONTRACT_SCHEMA.split(".")[0])
    extensions = [_DeclaringClientExtension(major=server_major + 1)]

    async with Client(mcp, extensions=extensions) as client:
        with pytest.raises(MCPError) as exc_info:
            await client.call_tool("list_metrics", {})

    assert exc_info.value.code == INVALID_PARAMS
    spy.assert_not_called()


@pytest.mark.asyncio
async def test_minor_above_server_rejected(canonic_service: CanonicService) -> None:
    """S19 AC2 also covers a MINOR the server does not implement yet."""
    mcp = build_server(canonic_service)
    server_major, server_minor = (int(p) for p in CONTRACT_SCHEMA.split("."))
    extensions = [_DeclaringClientExtension(major=server_major, minor=server_minor + 1)]

    async with Client(mcp, extensions=extensions) as client:
        with pytest.raises(MCPError) as exc_info:
            await client.call_tool("list_metrics", {})

    assert exc_info.value.code == INVALID_PARAMS


@pytest.mark.asyncio
async def test_no_declaration_is_served_normally(canonic_service: CanonicService) -> None:
    """S19 AC3: a client that does not opt in gets the call served, unaffected."""
    mcp = build_server(canonic_service)

    async with Client(mcp) as client:
        result = await client.call_tool("list_metrics", {})

    assert "metrics" in result.data


@pytest.mark.asyncio
async def test_matching_declaration_is_served(canonic_service: CanonicService) -> None:
    server_major, server_minor = (int(p) for p in CONTRACT_SCHEMA.split("."))
    extensions = [_DeclaringClientExtension(major=server_major, minor=server_minor)]
    mcp = build_server(canonic_service)

    async with Client(mcp, extensions=extensions) as client:
        result = await client.call_tool("list_metrics", {})

    assert "metrics" in result.data


@pytest.mark.asyncio
async def test_extension_schema_matches_contract_info_fallback(
    canonic_service: CanonicService,
) -> None:
    """S19 AC4: the extension's advertised schema matches the contract_info() fallback tool."""
    mcp = build_server(canonic_service)

    async with Client(mcp) as client:
        info = (await client.call_tool("contract_info", {})).data

    assert info["contract_schema"] == ContractExtension().settings()["schema"]
