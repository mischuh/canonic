"""Shared HTTP client settings for the connectors that call remote APIs over ``httpx``.

``httpx`` is an optional dependency and imported lazily by each connector, so the
timeout is built on demand rather than as a module constant.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import httpx

__all__ = ["CONNECT_TIMEOUT_S", "READ_TIMEOUT_S", "default_timeout"]

#: Seconds to establish a connection before giving up.
CONNECT_TIMEOUT_S = 10.0
#: Seconds to wait for response bytes and to send a request. httpx defaults to 5, which is
#: tight for a full Looker/Metabase listing.
READ_TIMEOUT_S = 30.0


def default_timeout() -> httpx.Timeout:
    """The explicit timeout every connector HTTP client is created with."""
    import httpx

    return httpx.Timeout(READ_TIMEOUT_S, connect=CONNECT_TIMEOUT_S)
