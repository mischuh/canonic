"""Semantic query — the protocol-neutral compiler input (SPEC-E5-E15 §3).

Adapters (MCP/CLI) produce this; the compiler never sees plain language. The query
references **names** (metrics, dimensions), never physical tables/columns — those are
resolved by the compiler against bindings and semantic sources.
"""

from __future__ import annotations

from datetime import datetime  # noqa: TC003 — Pydantic resolves field annotations at runtime
from typing import Any

from pydantic import BaseModel, ConfigDict, field_validator

__all__ = ["SemanticQuery"]

_OPERATOR_MAP: dict[str, str] = {
    "EQUALS": "=",
    "=": "=",
    "==": "=",
    "NOT_EQUALS": "!=",
    "!=": "!=",
    "<>": "!=",
    "GREATER_THAN": ">",
    ">": ">",
    "LESS_THAN": "<",
    "<": "<",
    "GREATER_THAN_OR_EQUAL": ">=",
    ">=": ">=",
    "LESS_THAN_OR_EQUAL": "<=",
    "<=": "<=",
    "LIKE": "LIKE",
    "IN": "IN",
}


def _quote_value(value: Any) -> str:
    if isinstance(value, str):
        escaped = value.replace("'", "''")
        return f"'{escaped}'"
    return str(value)


def _dict_to_predicate(d: dict[str, Any]) -> str:
    """Convert a structured filter dict to a SQL predicate string."""
    field = d.get("field")
    raw_op = str(d.get("operator", "")).upper()
    value = d.get("value")

    if not field:
        raise ValueError(f"filter dict missing 'field': {d}")
    sql_op = _OPERATOR_MAP.get(raw_op)
    if sql_op is None:
        raise ValueError(
            f"unknown filter operator {raw_op!r}; supported: {', '.join(_OPERATOR_MAP)}"
        )
    if sql_op == "IN":
        if not isinstance(value, list):
            raise ValueError(f"filter operator IN requires a list value, got {type(value)}")
        items = ", ".join(_quote_value(v) for v in value)
        return f"{field} IN ({items})"
    return f"{field} {sql_op} {_quote_value(value)}"


class SemanticQuery(BaseModel):
    """A resolved-by-name request the compiler turns into dialect-correct SQL (§3)."""

    model_config = ConfigDict(frozen=True)

    metrics: list[str]  # [P0] canonical metric names/aliases
    dimensions: list[str] = []  # [P0] dimension names to group by
    filters: list[str] = []  # [P0] predicate strings over dimension/column names
    via: list[str] = []  # [P0] intermediate source names to route join paths through
    context: str | None = None  # [P1] tag activating context-scoped guardrails
    limit: int | None = None  # [P0] row cap injected by the dialect adapter
    as_of: datetime | None = None  # [P1] reference point for finality watermark evaluation

    @field_validator("filters", mode="before")
    @classmethod
    def _coerce_filters(cls, v: Any) -> list[str]:
        if not isinstance(v, list):
            raise ValueError("filters must be a list")
        result: list[str] = []
        for item in v:
            if isinstance(item, str):
                result.append(item)
            elif isinstance(item, dict):
                result.append(_dict_to_predicate(item))
            else:
                raise ValueError(f"filter items must be str or dict, got {type(item).__name__}")
        return result
