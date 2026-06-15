"""Core capability response models (SPEC-E7-E8 §2.2).

These are the transport-neutral results the core returns; MCP and CLI adapters
serialize them verbatim. ``QueryResult`` merges the E5 compiler metadata and the
E2 ``ResultSet`` into the single object an agent answer depends on (§2.2), with no
field renamed by any surface.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict

from canon.connectors.base import (
    ResultSet,  # noqa: TC001 — Pydantic resolves field annotations at runtime
)

if TYPE_CHECKING:
    from canon.compiler.result import CompileResult

__all__ = [
    "Compiled",
    "FiredGuardrailOut",
    "MetricDetail",
    "MetricSummary",
    "QueryMetadata",
    "QueryResult",
    "SourceFreshnessOut",
]


class FiredGuardrailOut(BaseModel):
    """A guardrail that fired during compilation (mirrors compiler ``FiredGuardrail``)."""

    model_config = ConfigDict(frozen=True)

    id: str
    kind: str


class SourceFreshnessOut(BaseModel):
    """Per-source freshness metadata (mirrors compiler ``SourceFreshness``)."""

    model_config = ConfigDict(frozen=True)

    source: str
    last_validated_at: str | None = None
    stale: bool = False


class MetricSummary(BaseModel):
    """One active canonical metric, as returned by ``list_metrics`` (SPEC §4.1)."""

    model_config = ConfigDict(frozen=True)

    metric: str
    source: str
    measure: str
    status: str
    aliases: list[str] = []


class MetricDetail(BaseModel):
    """Grain, dimensions, owning source, and freshness for one metric (SPEC §4.1)."""

    model_config = ConfigDict(frozen=True)

    metric: str
    source: str
    measure: str
    grain: list[str]
    dimensions: list[str]
    measures: list[str]
    aliases: list[str] = []
    freshness: SourceFreshnessOut | None = None


class Compiled(BaseModel):
    """The compiled SQL and its dialect (SPEC §2.2 ``compiled``)."""

    model_config = ConfigDict(frozen=True)

    sql: str
    dialect: str


class QueryMetadata(BaseModel):
    """The E5 compiler metadata block carried alongside a result (SPEC §2.2)."""

    model_config = ConfigDict(frozen=True)

    resolved: dict[str, dict[str, str]]
    guardrails_fired: list[FiredGuardrailOut]
    freshness: list[SourceFreshnessOut]

    @classmethod
    def from_compile_result(cls, compiled: CompileResult) -> QueryMetadata:
        """Project a :class:`CompileResult` onto the §2.2 metadata shape."""
        return cls(
            resolved={"metrics": dict(compiled.resolved)},
            guardrails_fired=[
                FiredGuardrailOut(id=g.id, kind=g.kind) for g in compiled.guardrails_fired
            ],
            freshness=[
                SourceFreshnessOut(
                    source=f.source, last_validated_at=f.last_validated_at, stale=f.stale
                )
                for f in compiled.freshness
            ],
        )


class QueryResult(BaseModel):
    """The combined ``query`` response: ``result`` + ``compiled`` + ``metadata`` (SPEC §2.2)."""

    model_config = ConfigDict(frozen=True)

    result: ResultSet
    compiled: Compiled
    metadata: QueryMetadata

    @classmethod
    def from_parts(cls, compiled: CompileResult, result: ResultSet) -> QueryResult:
        """Merge the compiler output and the executed result set (no field renamed)."""
        return cls(
            result=result,
            compiled=Compiled(sql=compiled.sql, dialect=compiled.dialect),
            metadata=QueryMetadata.from_compile_result(compiled),
        )
