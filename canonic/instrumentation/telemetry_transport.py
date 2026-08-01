"""Network transport for opt-in aggregate telemetry (SPEC-E16 §8/§12).

:mod:`canonic.instrumentation.telemetry` defines exactly what payload *would* be sent;
this module is what actually sends it. A send is only attempted after
:func:`canonic.airgap.guard_telemetry_send` passes — ``telemetry.enabled``,
``telemetry.endpoint``, and ``telemetry.transport_acknowledged`` must all be set, and
``runtime.air_gapped`` must be false. In particular, ``transport_acknowledged`` is a
human attestation that the exact aggregate payload has been privacy-reviewed; canonic
cannot verify that on its own, so setting ``enabled: true`` alone still sends nothing.

Exactly one attempt is made per call — no retry. Sending is an explicit, low-frequency,
user-triggered CLI action (``canonic report --telemetry-send``), not an automatic call
buried in a serving path, so a failed attempt is simply retried by re-running the command.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from canonic.airgap import guard_telemetry_send
from canonic.exc import TelemetrySendError

__all__ = ["HttpTelemetrySender", "TelemetrySender", "send_telemetry"]


@runtime_checkable
class TelemetrySender(Protocol):
    """DI seam for sending one telemetry payload — mirrors the connectors' fetch seams."""

    async def send(
        self, endpoint: str, payload: dict[str, Any], *, auth_token: str | None = None
    ) -> None: ...


class HttpTelemetrySender:
    """Default sender — POSTs the payload as JSON via ``httpx``.

    ``httpx`` is imported lazily so the module can be imported without it installed;
    add ``httpx>=0.27`` to project dependencies before using this class (same pattern
    as :class:`canonic.connectors.web.HttpUrlPageSource`).
    """

    async def send(
        self, endpoint: str, payload: dict[str, Any], *, auth_token: str | None = None
    ) -> None:
        try:
            import httpx
        except ImportError as exc:
            raise ImportError(
                "httpx is required for telemetry sending; add httpx>=0.27 to project dependencies"
            ) from exc

        headers = {"Authorization": f"Bearer {auth_token}"} if auth_token else {}
        async with httpx.AsyncClient() as client:
            try:
                resp = await client.post(endpoint, json=payload, headers=headers)
                resp.raise_for_status()
            except httpx.HTTPError as exc:
                raise TelemetrySendError(f"telemetry send to {endpoint!r} failed: {exc}") from exc


async def send_telemetry(
    payload: dict[str, Any],
    *,
    air_gapped: bool,
    telemetry_enabled: bool,
    endpoint: str | None,
    transport_acknowledged: bool,
    auth_token: str | None = None,
    sender: TelemetrySender | None = None,
) -> None:
    """Send ``payload`` if and only if fully authorized; otherwise raise.

    Re-checks :func:`~canonic.airgap.guard_telemetry_send` here (in addition to any
    check the caller already did) so a direct import of this function can never bypass
    the gate — defense in depth, mirroring how ``EgressPolicy.check_url`` is re-checked
    immediately before every LLM call in :mod:`canonic.runtime.generation`.
    """
    guard_telemetry_send(
        air_gapped=air_gapped,
        telemetry_enabled=telemetry_enabled,
        endpoint=endpoint,
        transport_acknowledged=transport_acknowledged,
    )
    assert endpoint is not None  # guaranteed by guard_telemetry_send above
    await (sender or HttpTelemetrySender()).send(endpoint, payload, auth_token=auth_token)
