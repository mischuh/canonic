"""No sensitive data in gateway routing headers (AMENDMENT-fastmcp4-adoption §6, S23).

On the sessionless era a client attaches ``Mcp-Method``/``Mcp-Name`` as plain HTTP headers so
a gateway can route without parsing the JSON-RPC body. Canonic opts in to nothing beyond
those two, because SQL text and metric/filter values would otherwise land in proxy and
ingress access logs, a channel E12's masking policy cannot see. This test inspects the raw
outgoing requests rather than trusting the tool schemas.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import httpx2
import pytest
from fastmcp import Client
from fastmcp.client.transports.http import StreamableHttpTransport
from fastmcp.utilities.asgi_transport import StreamingASGITransport
from fastmcp.utilities.tests import asgi_server

from canonic.core.service import CanonicService
from canonic.mcp.server import build_server

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.integration

_SQL_MARKER = "routing_leak_marker_7391"


class _RecordingTransport(httpx2.AsyncBaseTransport):
    """Forwards to the app and keeps every outgoing request's headers."""

    def __init__(self, inner: StreamingASGITransport) -> None:
        self._inner = inner
        self.headers: list[httpx2.Headers] = []

    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        self.headers.append(request.headers)
        return await self._inner.handle_async_request(request)


@pytest.mark.asyncio
async def test_run_sql_call_leaks_no_argument_value_into_headers(report_project: Path) -> None:
    """S23 AC1: no request header carries SQL text, metric names or filter values."""
    server = build_server(CanonicService.from_project(report_project))

    async with (
        asgi_server(server, stateless_http=True) as served,
        StreamingASGITransport(served.app) as inner,
    ):
        recorder = _RecordingTransport(inner)

        def client_factory(
            headers: dict[str, str] | None = None,
            timeout: httpx2.Timeout | None = None,
            auth: httpx2.Auth | None = None,
            **kwargs: Any,
        ) -> httpx2.AsyncClient:
            return httpx2.AsyncClient(
                transport=recorder, headers=headers, timeout=timeout, auth=auth, **kwargs
            )

        transport = StreamableHttpTransport(url=served.url, httpx_client_factory=client_factory)
        async with Client(transport, mode="2026-07-28") as client:
            await client.call_tool(
                "run_sql",
                {"sql": f"SELECT '{_SQL_MARKER}' AS marker, 'revenue' AS metric"},
                raise_on_error=False,
            )

    assert recorder.headers, "no request was captured"
    for headers in recorder.headers:
        joined = " ".join(f"{k}: {v}" for k, v in headers.items()).lower()
        assert _SQL_MARKER not in joined
        assert "select" not in joined
        assert "revenue" not in joined
        assert not [k for k in headers if k.lower().startswith("mcp-param-")]
