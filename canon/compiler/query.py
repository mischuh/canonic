"""Semantic query — the protocol-neutral compiler input (SPEC-E5-E15 §3).

Adapters (MCP/CLI) produce this; the compiler never sees plain language. The query
references **names** (metrics, dimensions), never physical tables/columns — those are
resolved by the compiler against bindings and semantic sources.
"""

from __future__ import annotations

from datetime import datetime  # noqa: TC003 — Pydantic resolves field annotations at runtime

from pydantic import BaseModel, ConfigDict

__all__ = ["SemanticQuery"]


class SemanticQuery(BaseModel):
    """A resolved-by-name request the compiler turns into dialect-correct SQL (§3)."""

    model_config = ConfigDict(frozen=True)

    metrics: list[str]  # [P0] canonical metric names/aliases
    dimensions: list[str] = []  # [P0] dimension names to group by
    filters: list[str] = []  # [P0] predicate strings over dimension/column names
    context: str | None = None  # [P1] tag activating context-scoped guardrails
    limit: int | None = None  # [P0] row cap injected by the dialect adapter
    as_of: datetime | None = None  # [P1] reference point for finality watermark evaluation
