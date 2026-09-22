"""Sessionless requests need no session affinity — AMENDMENT-fastmcp4-adoption §1.4, S18 AC3.

On the sessionless era every request is self-contained, so two ``http`` replicas behind
an ordinary round-robin load balancer can each serve any request. This is the property
that removes session affinity as a prerequisite for scaling the ``http`` profile
horizontally, and it is worth pinning because nothing in a single-replica test would
notice if a future change reintroduced per-session server state.

The two replicas are two independent ``build_server`` instances served in-process. No
socket and no uvicorn, but the full HTTP stack (middleware, auth, session handling)
runs exactly as in production, and the load balancer is a transport that alternates
replicas per request rather than per connection.
"""

from __future__ import annotations

import contextlib
import itertools
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

_CALLS = 100


class _RoundRobinBalancer(httpx2.AsyncBaseTransport):
    """Dispatches each request to the next replica, with no affinity of any kind."""

    def __init__(self, replicas: list[StreamingASGITransport]) -> None:
        self._replicas = replicas
        self._counter = itertools.count()
        self.dispatched: list[int] = []

    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        index = next(self._counter) % len(self._replicas)
        self.dispatched.append(index)
        return await self._replicas[index].handle_async_request(request)


@pytest.mark.release_gate
@pytest.mark.asyncio
async def test_sessionless_client_survives_round_robin_across_replicas(
    report_project: Path,
) -> None:
    """S18 AC3: 100 consecutive ``query`` calls across two replicas, no affinity, no error."""
    replica_a = build_server(CanonicService.from_project(report_project))
    replica_b = build_server(CanonicService.from_project(report_project))

    async with (
        asgi_server(replica_a, stateless_http=True) as served_a,
        asgi_server(replica_b, stateless_http=True) as served_b,
        contextlib.AsyncExitStack() as stack,
    ):
        replicas = [
            await stack.enter_async_context(StreamingASGITransport(served.app))
            for served in (served_a, served_b)
        ]
        balancer = _RoundRobinBalancer(replicas)

        def client_factory(
            headers: dict[str, str] | None = None,
            timeout: httpx2.Timeout | None = None,
            auth: httpx2.Auth | None = None,
            **kwargs: Any,
        ) -> httpx2.AsyncClient:
            return httpx2.AsyncClient(
                transport=balancer, headers=headers, timeout=timeout, auth=auth, **kwargs
            )

        transport = StreamableHttpTransport(url=served_a.url, httpx_client_factory=client_factory)
        async with Client(transport, mode="2026-07-28") as client:
            rows = [
                (
                    await client.call_tool(
                        "query", {"query": {"metrics": ["revenue"], "dimensions": ["status"]}}
                    )
                ).data["result"]["rows"]
                for _ in range(_CALLS)
            ]

    assert rows == [[["paid", "100.00"]]] * _CALLS
    assert set(balancer.dispatched) == {0, 1}, "load balancer never reached both replicas"
