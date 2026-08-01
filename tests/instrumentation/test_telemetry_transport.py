"""Tests for send_telemetry / HttpTelemetrySender (SPEC-E16 §8/§12).

Uses the ``TelemetrySender`` DI seam (mirrors ``UrlPageSource`` in
tests/connectors/test_web.py) so these tests need no network access and no ``httpx``
installed.
"""

from __future__ import annotations

from typing import Any

import pytest

from canonic.exc import AirGappedViolation, CanonicError, TelemetryNotConfigured, TelemetrySendError
from canonic.instrumentation.telemetry_transport import HttpTelemetrySender, send_telemetry

_AUTHORIZED_KWARGS = {
    "air_gapped": False,
    "telemetry_enabled": True,
    "endpoint": "https://collector.example.com/ingest",
    "transport_acknowledged": True,
}


class FixtureTelemetrySender:
    """In-process sender recording calls, no network access."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any], str | None]] = []

    async def send(
        self, endpoint: str, payload: dict[str, Any], *, auth_token: str | None = None
    ) -> None:
        self.calls.append((endpoint, payload, auth_token))


async def test_send_telemetry_calls_sender_once_when_authorized() -> None:
    sender = FixtureTelemetrySender()
    payload = {"schema_version": "1", "answer_count": 3}

    await send_telemetry(payload, sender=sender, **_AUTHORIZED_KWARGS)

    assert sender.calls == [("https://collector.example.com/ingest", payload, None)]


async def test_send_telemetry_forwards_auth_token() -> None:
    sender = FixtureTelemetrySender()
    payload = {"schema_version": "1"}

    await send_telemetry(payload, sender=sender, auth_token="secret-token", **_AUTHORIZED_KWARGS)

    assert sender.calls == [("https://collector.example.com/ingest", payload, "secret-token")]


@pytest.mark.parametrize(
    ("override", "expected"),
    [
        ({"telemetry_enabled": False}, TelemetryNotConfigured),
        ({"endpoint": None}, TelemetryNotConfigured),
        ({"transport_acknowledged": False}, TelemetryNotConfigured),
        ({"air_gapped": True}, AirGappedViolation),
    ],
)
async def test_send_telemetry_never_calls_sender_when_not_authorized(
    override: dict[str, Any], expected: type[CanonicError]
) -> None:
    sender = FixtureTelemetrySender()
    payload = {"schema_version": "1"}
    kwargs = {**_AUTHORIZED_KWARGS, **override}

    with pytest.raises(expected):
        await send_telemetry(payload, sender=sender, **kwargs)

    assert sender.calls == []


async def test_http_telemetry_sender_raises_telemetry_send_error_on_http_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    httpx = pytest.importorskip("httpx")

    def _boom(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, request=request)

    transport = httpx.MockTransport(_boom)
    real_async_client = httpx.AsyncClient

    def _client_factory(*args: Any, **kwargs: Any) -> Any:
        kwargs["transport"] = transport
        return real_async_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", _client_factory)

    with pytest.raises(TelemetrySendError):
        await HttpTelemetrySender().send("https://collector.example.com/ingest", {"a": 1})
