"""Return types for the E10 generation runtime (SPEC-E10 §8)."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict


class Usage(BaseModel):
    """Per-call usage metrics for E16 (SPEC-E10 §8).

    Token fields are best-effort (``None`` when the endpoint omits them); ``calls`` and
    ``latency_ms`` are always measured locally and are always present.
    """

    model_config = ConfigDict(frozen=True)

    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    total_tokens: int | None = None
    calls: int
    latency_ms: float


class Completion(BaseModel):
    """The result of one generation call (SPEC-E10 §8).

    ``parsed`` is populated only when the caller requested structured output, in which
    case it carries the validated JSON payload (a ``model_dump`` of the response model);
    otherwise the caller works from ``text``. ``model`` records the resolved model string
    that was actually called, so callers can audit task → model resolution. ``usage``
    carries token/call/latency metrics for E16 to log alongside warehouse ``bytes_scanned``.
    """

    model_config = ConfigDict(frozen=True)

    text: str
    parsed: dict[str, Any] | None = None
    model: str
    usage: Usage
