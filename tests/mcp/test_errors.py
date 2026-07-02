"""Tests for MCP error serialisation (canonic/mcp/errors.py)."""

from __future__ import annotations

import pytest

from canonic.exc import Ambiguous, CanonicError, Unresolved
from canonic.mcp.errors import canonic_error_response, error_payload


def test_error_payload_unresolved() -> None:
    exc = Unresolved("metric 'foo' matches no active binding")
    payload = error_payload(exc)
    assert payload["code"] == "unresolved"
    assert "foo" in payload["message"]
    assert "candidates" not in payload


def test_error_payload_ambiguous_with_candidates() -> None:
    exc = Ambiguous("metric 'bar' is ambiguous", candidates=["a", "b"])
    payload = error_payload(exc)
    assert payload["code"] == "ambiguous"
    assert payload["candidates"] == ["a", "b"]


def test_error_payload_no_code() -> None:
    exc = CanonicError("something internal")
    payload = error_payload(exc)
    assert payload["code"] == "internal_error"


@pytest.mark.asyncio
async def test_canonic_error_response_wraps_canonic_error() -> None:
    @canonic_error_response
    async def failing_tool() -> dict:
        raise Unresolved("metric 'x' matches no active binding")

    result = await failing_tool()
    assert result["code"] == "unresolved"
    assert "candidates" not in result


@pytest.mark.asyncio
async def test_canonic_error_response_passes_through_success() -> None:
    @canonic_error_response
    async def succeeding_tool() -> dict:
        return {"ok": True}

    result = await succeeding_tool()
    assert result == {"ok": True}


@pytest.mark.asyncio
async def test_canonic_error_response_reraises_non_canonic() -> None:
    @canonic_error_response
    async def broken_tool() -> dict:
        raise ValueError("not a canonic error")

    with pytest.raises(ValueError, match="not a canonic error"):
        await broken_tool()
