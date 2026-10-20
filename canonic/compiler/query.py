"""Semantic query — the protocol-neutral compiler input (SPEC-E5-E15 §3).

Adapters (MCP/CLI) produce this; the compiler never sees plain language. The query
references **names** (metrics, dimensions), never physical tables/columns — those are
resolved by the compiler against bindings and semantic sources.
"""

from __future__ import annotations

import re
from datetime import date, datetime  # noqa: TC003 — Pydantic resolves field annotations at runtime
from typing import Any

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from canonic.semantic.duration import Duration, DurationField, DurationUnit

__all__ = [
    "SemanticQuery",
    "TimeRange",
    "coerce_filters",
    "parse_filter_flag",
    "parse_time_range_flag",
]

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


def _coerce_scalar(text: str) -> Any:
    """Coerce a raw CLI token to ``int``/``float`` when it looks numeric, else ``str``.

    Mirrors what a hand-written JSON filter dict would carry (``{"value": 100}``,
    not ``{"value": "100"}``) so a numeric comparison compiles to an unquoted SQL
    literal, matching the JSON-file path (§ AC2).
    """
    try:
        return int(text)
    except ValueError:
        pass
    try:
        return float(text)
    except ValueError:
        return text


_BARE_OP_PATTERN = re.compile(r">=|<=|!=|<>|==|>|<|=")


def parse_filter_flag(raw: str) -> str:
    """Parse a CLI ``--filter`` value into a SQL predicate string.

    Accepts ``field<op>value`` with ``<op>`` inlined (e.g. ``units_sold>=3``,
    ``status=paid``) or ``field:op:value`` where ``op`` is one of the keys in
    :data:`_OPERATOR_MAP` (e.g. ``amount:>:100``, ``status:!=:refunded``,
    ``status:in:paid,refunded``). Both forms build the same dict shape the
    JSON-file path already accepts and go through :func:`_dict_to_predicate`,
    so there is exactly one filter grammar.

    The inline form matches the *leftmost* operator occurrence, preferring
    two-character operators (``>=``, ``<=``, ``!=``, ``<>``, ``==``) over their
    single-character prefix so ``field>=value`` isn't misread as ``field>`` ``=``
    ``value``. A bare value containing ``=`` (e.g. an email address) still
    round-trips: none of the two-character operators match at that position, so
    it falls back to splitting on the lone ``=``.
    """
    head, sep, tail = raw.partition(":")
    if sep:
        op_candidate, sep2, value_raw = tail.partition(":")
        if sep2:
            op = op_candidate.strip().upper()
            value: Any = (
                [_coerce_scalar(v.strip()) for v in value_raw.split(",")]
                if op == "IN"
                else _coerce_scalar(value_raw)
            )
            return _dict_to_predicate({"field": head.strip(), "operator": op, "value": value})

    match = _BARE_OP_PATTERN.search(raw)
    if not match:
        raise ValueError(f"filter must be field=value or field:op:value, got {raw!r}")
    field = raw[: match.start()].strip()
    value_str = raw[match.end() :]
    return _dict_to_predicate(
        {"field": field, "operator": match.group(), "value": _coerce_scalar(value_str)}
    )


def coerce_filters(v: Any) -> list[str]:
    """Normalize a filter list: predicate strings pass through, dicts become predicates."""
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


class TimeRange(BaseModel):
    """A typed range on one time dimension: the last N units before ``as_of``, or two dates.

    ``last`` counts whole units before the unit that contains the anchor day, so ``last: 3
    months`` on 20 October is July to September. ``include_current`` extends the range to the
    end of the anchor day. ``start`` is inclusive and ``end`` exclusive. Bounds are resolved to
    dates at compile time (:func:`canonic.compiler.time_range.resolve_time_range`).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    dimension: str
    last: DurationField | None = None
    start: date | None = None
    end: date | None = None
    include_current: bool = False

    @model_validator(mode="after")
    def _validate_bounds(self) -> TimeRange:
        explicit = self.start is not None or self.end is not None
        if (self.last is None) == (not explicit):
            raise ValueError("time_range sets exactly one of 'last' and 'start'/'end'")
        if self.last is not None and self.last.unit.rank < DurationUnit.DAY.rank:
            raise ValueError(f"time_range.last must be a day or longer, got {self.last}")
        if self.start is not None and self.end is not None and self.start >= self.end:
            raise ValueError(f"time_range.start {self.start} must be before end {self.end}")
        if self.include_current and self.last is None:
            raise ValueError("time_range.include_current requires 'last'")
        return self


def parse_time_range_flag(raw: str) -> TimeRange:
    """Parse a CLI ``--time-range`` value.

    ``order_date:last:90d`` (with an optional ``:current`` suffix for ``include_current``) or
    ``order_date:2026-01-01..2026-04-01``, where either date may be left out.
    """
    dimension, sep, rest = raw.partition(":")
    if not sep or not dimension.strip():
        raise ValueError(
            f"time range must be dimension:last:<duration> or dimension:<start>..<end>, got {raw!r}"
        )
    dimension = dimension.strip()
    if rest.startswith("last:"):
        duration, _, flag = rest.removeprefix("last:").partition(":")
        if flag not in ("", "current"):
            raise ValueError(f"unknown time range suffix {flag!r}; the only one is 'current'")
        return TimeRange(
            dimension=dimension, last=Duration.parse(duration), include_current=flag == "current"
        )
    start, dots, end = rest.partition("..")
    if not dots:
        raise ValueError(
            f"time range must be dimension:last:<duration> or dimension:<start>..<end>, got {raw!r}"
        )
    return TimeRange(
        dimension=dimension,
        start=date.fromisoformat(start) if start else None,
        end=date.fromisoformat(end) if end else None,
    )


class SemanticQuery(BaseModel):
    """A resolved-by-name request the compiler turns into dialect-correct SQL (§3)."""

    model_config = ConfigDict(frozen=True)

    metrics: list[str]  # [P0] canonical metric names/aliases
    dimensions: list[str] = []  # [P0] dimension names to group by
    filters: list[str] = []  # [P0] predicate strings over dimension/column names
    via: list[str] = []  # [P0] intermediate source names to route join paths through
    context: str | None = None  # [P1] tag activating context-scoped guardrails
    limit: int | None = None  # [P0] row cap injected by the dialect adapter
    as_of: datetime | None = None  # [P1] reference point for finality and time_range
    time_range: TimeRange | None = None  # [P1] typed range on one time dimension

    @field_validator("filters", mode="before")
    @classmethod
    def _coerce_filters(cls, v: Any) -> list[str]:
        return coerce_filters(v)
