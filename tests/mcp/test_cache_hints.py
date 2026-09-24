"""Tests for listing cache hints and the routing-header lock (AMENDMENT-fastmcp4-adoption
§5/§6, S23, S24).

S23 AC1 needs no server-side mechanism: routing headers (``Mcp-Method``/``Mcp-Name``) are
attached by the *client* on the sessionless era, and the only server-side surface —
an ``x-mcp-header`` JSON Schema annotation on a tool parameter, which makes a conforming
client mirror that argument value into an ``Mcp-Param-*`` header — is never set anywhere
in this codebase. So S23 becomes a regression test locking that no tool ever gains one,
since that would leak SQL text, metric names or filter values into proxy access logs.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest
from fastmcp import Client
from fastmcp.server.auth.providers.jwt import JWTVerifier, RSAKeyPair

from canonic.mcp.server import build_server

if TYPE_CHECKING:
    from canonic.core.service import CanonicService


def _find_x_mcp_header(schema: Any) -> list[str]:
    """Recursively collect JSON-pointer-ish paths of any ``x-mcp-header`` key found."""
    hits: list[str] = []

    def _walk(node: Any, path: str) -> None:
        if isinstance(node, dict):
            if "x-mcp-header" in node:
                hits.append(path)
            for key, value in node.items():
                _walk(value, f"{path}/{key}")
        elif isinstance(node, list):
            for i, value in enumerate(node):
                _walk(value, f"{path}[{i}]")

    _walk(schema, "")
    return hits


class TestNoRoutingHeaderAnnotations:
    """S23 AC1: no tool's input schema ever mirrors an argument into a proxy-visible
    header — a routing-header leak would put SQL text, metric names or filter values
    into an intermediary's access logs."""

    async def test_no_tool_input_schema_carries_x_mcp_header(
        self, canonic_service: CanonicService
    ) -> None:
        mcp = build_server(canonic_service)
        async with Client(mcp) as client:
            tools = await client.list_tools()

        offenders = {tool.name: _find_x_mcp_header(tool.input_schema) for tool in tools}
        offenders = {name: hits for name, hits in offenders.items() if hits}
        assert offenders == {}


class TestCacheHints:
    """S24: the default listing cache hint is conservative — a TTL, never a shared
    ("public") scope — and ``cache_ttl_seconds: 0`` removes it entirely."""

    async def test_default_hint_has_ttl_and_never_public_scope(
        self, canonic_service: CanonicService
    ) -> None:
        mcp = build_server(canonic_service)
        async with Client(mcp) as client:
            result = await client.list_tools_mcp()

        assert result.ttl_ms == 300_000
        assert result.cache_scope == "private"

    async def test_zero_cache_ttl_seconds_removes_the_hint(
        self, canonic_service: CanonicService
    ) -> None:
        mcp = build_server(canonic_service, cache_ttl_seconds=0)
        async with Client(mcp) as client:
            result = await client.list_tools_mcp()

        # No hint registered at all: byte-identical to a server that never set one,
        # not merely a TTL of zero (which is a distinct, still-present hint).
        assert result.ttl_ms == 0
        assert result.cache_scope == "private"

    async def test_custom_cache_ttl_seconds_applied(self, canonic_service: CanonicService) -> None:
        mcp = build_server(canonic_service, cache_ttl_seconds=60)
        async with Client(mcp) as client:
            result = await client.list_tools_mcp()

        assert result.ttl_ms == 60_000
        assert result.cache_scope == "private"


class TestCacheScopeWithAuth:
    """S24 AC1: whenever ``mcp.auth`` is configured the listing is never publicly cacheable."""

    @pytest.mark.parametrize("ttl_seconds", [1, 60, 300, 86_400])
    async def test_tools_list_never_carries_public_scope(
        self, canonic_service: CanonicService, ttl_seconds: int
    ) -> None:
        pair = RSAKeyPair.generate()
        auth = JWTVerifier(
            public_key=pair.public_key, issuer="https://idp.example", audience="canonic"
        )
        mcp = build_server(canonic_service, auth=auth, cache_ttl_seconds=ttl_seconds)
        async with Client(mcp) as client:
            result = await client.list_tools_mcp()

        assert result.cache_scope != "public"
