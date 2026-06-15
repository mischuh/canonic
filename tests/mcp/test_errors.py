"""Tests for MCP error serialisation (canon/mcp/errors.py)."""

from __future__ import annotations

import pytest

from canon.exc import Ambiguous, CanonError, Unresolved
from canon.mcp.errors import canon_error_response, error_payload


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
    exc = CanonError("something internal")
    payload = error_payload(exc)
    assert payload["code"] == "internal_error"


@pytest.mark.asyncio
async def test_canon_error_response_wraps_canon_error() -> None:
    @canon_error_response
    async def failing_tool() -> dict:
        raise Unresolved("metric 'x' matches no active binding")

    result = await failing_tool()
    assert result["code"] == "unresolved"
    assert "candidates" not in result


@pytest.mark.asyncio
async def test_canon_error_response_passes_through_success() -> None:
    @canon_error_response
    async def succeeding_tool() -> dict:
        return {"ok": True}

    result = await succeeding_tool()
    assert result == {"ok": True}


@pytest.mark.asyncio
async def test_canon_error_response_reraises_non_canon() -> None:
    @canon_error_response
    async def broken_tool() -> dict:
        raise ValueError("not a canon error")

    with pytest.raises(ValueError, match="not a canon error"):
        await broken_tool()
