"""Kubernetes-style probes: ``/livez`` and ``/readyz`` are unauthenticated and shallow."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from fastmcp.utilities.tests import asgi_server

from canonic import __version__ as CANONIC_VERSION
from canonic.core.service import CanonicService
from canonic.mcp.auth import CanonicTokenVerifier, ResolvedToken
from canonic.mcp.server import build_server

if TYPE_CHECKING:
    from pathlib import Path

    from fastmcp import FastMCP

pytestmark = pytest.mark.integration


def _origin(mcp_url: str) -> str:
    """The server origin: ``served.url`` ends in ``/mcp`` and relative URLs resolve against it."""
    return mcp_url.removesuffix("/mcp")


@pytest.fixture
def secured_server(report_project: Path) -> FastMCP:
    """A server with bearer-token auth, so unauthenticated access to ``/mcp`` is refused."""
    verifier = CanonicTokenVerifier({"s3cret": ResolvedToken(client_id="alice")})
    return build_server(CanonicService.from_project(report_project), auth=verifier)


@pytest.mark.asyncio
async def test_probes_answer_without_credentials(secured_server: FastMCP) -> None:
    async with (
        asgi_server(secured_server, stateless_http=True) as served,
        served.http_client() as http,
    ):
        livez = await http.get(f"{_origin(served.url)}/livez")
        readyz = await http.get(f"{_origin(served.url)}/readyz")

    assert livez.status_code == 200
    assert livez.json() == {"status": "ok"}
    assert readyz.status_code == 200
    assert readyz.json() == {"status": "ready", "version": CANONIC_VERSION}


@pytest.mark.asyncio
async def test_probes_do_not_weaken_mcp_auth(secured_server: FastMCP) -> None:
    async with (
        asgi_server(secured_server, stateless_http=True) as served,
        served.http_client() as http,
    ):
        response = await http.post(served.url, json={})

    assert response.status_code == 401


@pytest.mark.asyncio
async def test_probes_reject_non_get(secured_server: FastMCP) -> None:
    async with (
        asgi_server(secured_server, stateless_http=True) as served,
        served.http_client() as http,
    ):
        assert (await http.post(f"{_origin(served.url)}/livez")).status_code == 405
        assert (await http.post(f"{_origin(served.url)}/readyz")).status_code == 405
