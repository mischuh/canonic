"""Return types for the E10 generation runtime (SPEC-E10 §8)."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict


class Completion(BaseModel):
    """The result of one generation call (SPEC-E10 §8).

    ``parsed`` is populated only when the caller requested structured output, in which
    case it carries the validated JSON payload (a ``model_dump`` of the response model);
    otherwise the caller works from ``text``. ``model`` records the resolved model string
    that was actually called, so callers can audit task → model resolution.
    """

    model_config = ConfigDict(frozen=True)

    text: str
    parsed: dict[str, Any] | None = None
    model: str
    # NOTE (#67): usage/token/latency metrics are intentionally omitted here; the full
    # runtime interface (GenerationRuntime + EmbeddingRuntime, usage metrics) lands in #67.
