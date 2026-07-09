"""Core capability response models (SPEC-E7-E8 §2.2).

These are the transport-neutral results the core returns; MCP and CLI adapters
serialize them verbatim. ``QueryResult`` merges the E5 compiler metadata and the
E2 ``ResultSet`` into the single object an agent answer depends on (§2.2), with no
field renamed by any surface.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict

from canonic.connectors.base import (
    ResultSet,  # noqa: TC001 — Pydantic resolves field annotations at runtime
)
from canonic.contract import CONTRACT_SCHEMA
from canonic.contracts.models import (
    Example,  # noqa: TC001 — Pydantic resolves field annotations at runtime
)
from canonic.trust.scorer import trust_for_compiled

if TYPE_CHECKING:
    from canonic.compiler.result import CompileResult

__all__ = [
    "Compiled",
    "CompileOutput",
    "DimensionInfo",
    "DomainGroup",
    "FinalityOut",
    "FiredGuardrailOut",
    "MetricDetail",
    "MetricRef",
    "MetricSummary",
    "OverviewResult",
    "QueryMetadata",
    "QueryResult",
    "RelatedDimensionOut",
    "RelatedMetricOut",
    "RelatedOut",
    "SourceFreshnessOut",
    "TrustScoreOut",
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


class FinalityOut(BaseModel):
    """Finality metadata block in a query response (SPEC-E5-E15 stage 8)."""

    model_config = ConfigDict(frozen=True)

    watermark: str
    sources_used: list[str]
    final_rows: int | None = None
    provisional_rows: int | None = None


class TrustScoreOut(BaseModel):
    """The ``metadata.trust_score`` block: tier + capping reasons (SPEC-E14 §3, §6)."""

    model_config = ConfigDict(frozen=True)

    tier: str
    reasons: list[str] = []


class RelatedDimensionOut(BaseModel):
    """An unused queryable dimension surfaced in ``metadata.related`` (SPEC-E7/E8 §2.2)."""

    model_config = ConfigDict(frozen=True)

    name: str
    source: str
    label: str | None = None


class RelatedMetricOut(BaseModel):
    """An active sibling metric surfaced in ``metadata.related`` (SPEC-E7/E8 §2.2)."""

    model_config = ConfigDict(frozen=True)

    name: str
    source: str


class RelatedOut(BaseModel):
    """The ``metadata.related`` block: unused dimensions and sibling metrics (SPEC-E7/E8 §2.2)."""

    model_config = ConfigDict(frozen=True)

    unused_dimensions: list[RelatedDimensionOut] = []
    sibling_metrics: list[RelatedMetricOut] = []


class DimensionInfo(BaseModel):
    """One queryable dimension as returned by ``describe_metric`` (SPEC §4.1).

    ``name`` is the canonical string to pass to ``query()`` or ``compile_query()``.
    ``source`` is the semantic source alias that owns the dimension in the join graph.
    ``label`` and ``description`` are optional author-supplied metadata for LLM reasoning.
    """

    model_config = ConfigDict(frozen=True)

    name: str
    source: str
    label: str | None = None
    description: str | None = None


class MetricSummary(BaseModel):
    """One active canonical metric, as returned by ``list_metrics`` (SPEC §4.1).

    For composite kinds (ratio, weighted_avg), ``source`` and ``measure`` are None;
    ``components`` holds the ordered component metric names [numerator, denominator].
    """

    model_config = ConfigDict(frozen=True)

    metric: str
    kind: str
    source: str | None = None
    measure: str | None = None
    status: str
    aliases: list[str] = []
    components: list[str] | None = None
    dimensions: list[DimensionInfo] = []


class MetricDetail(BaseModel):
    """Grain, dimensions, owning source, and freshness for one metric (SPEC §4.1)."""

    model_config = ConfigDict(frozen=True)

    metric: str
    source: str | None
    measure: str | None = None
    grain: list[str]
    dimensions: list[DimensionInfo]
    measures: list[str]
    aliases: list[str] = []
    freshness: SourceFreshnessOut | None = None
    examples: list[Example] = []


class MetricRef(BaseModel):
    """A metric reference carrying both its canonical name and a display label."""

    model_config = ConfigDict(frozen=True)

    name: str
    label: str


class DomainGroup(BaseModel):
    """Metrics grouped under one domain with sample questions (SPEC §4.1 S12)."""

    model_config = ConfigDict(frozen=True)

    name: str
    metrics: list[MetricRef]
    dimensions: list[str] = []
    sample_questions: list[str]


class OverviewResult(BaseModel):
    """Result of get_overview: metrics grouped by domain (SPEC §4.1 S12)."""

    model_config = ConfigDict(frozen=True)

    domains: list[DomainGroup]


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
    warnings: list[str] = []
    contract_schema: str = CONTRACT_SCHEMA
    finality: FinalityOut | None = None
    related: RelatedOut = RelatedOut()
    trust_score: TrustScoreOut | None = None

    @classmethod
    def from_compile_result(
        cls, compiled: CompileResult, result: ResultSet | None = None
    ) -> QueryMetadata:
        """Project a :class:`CompileResult` onto the §2.2 metadata shape.

        When ``result`` is provided and the compile result carries finality metadata,
        the ``is_final`` column in the result set is used to tally ``final_rows`` and
        ``provisional_rows``. Those tallies also feed the trust tier (SPEC-E14 §6):
        static signals (provenance, assertion coverage) apply regardless of ``result``,
        while the finality/freshness signals only activate once row-level data is known.
        """
        final_rows: int | None = None
        provisional_rows: int | None = None
        finality_out: FinalityOut | None = None
        if compiled.finality is not None:
            if result is not None:
                col_names = [c.name for c in result.columns]
                if "is_final" in col_names:
                    idx = col_names.index("is_final")
                    final_rows = sum(1 for row in result.rows if row[idx])
                    provisional_rows = len(result.rows) - final_rows
            finality_out = FinalityOut(
                watermark=compiled.finality.watermark,
                sources_used=compiled.finality.sources_used,
                final_rows=final_rows,
                provisional_rows=provisional_rows,
            )
        trust = trust_for_compiled(compiled, result)
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
            warnings=list(compiled.warnings),
            contract_schema=CONTRACT_SCHEMA,
            finality=finality_out,
            related=RelatedOut(
                unused_dimensions=[
                    RelatedDimensionOut(name=d.name, source=d.source, label=d.label)
                    for d in compiled.related.unused_dimensions
                ],
                sibling_metrics=[
                    RelatedMetricOut(name=m.name, source=m.source)
                    for m in compiled.related.sibling_metrics
                ],
            ),
            trust_score=TrustScoreOut(tier=trust.tier.value, reasons=list(trust.reasons)),
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
            metadata=QueryMetadata.from_compile_result(compiled, result=result),
        )


class CompileOutput(BaseModel):
    """The ``compile`` response: ``compiled`` + ``metadata`` without a result set (SPEC §2.2)."""

    model_config = ConfigDict(frozen=True)

    compiled: Compiled
    metadata: QueryMetadata

    @classmethod
    def from_compile_result(cls, compiled: CompileResult) -> CompileOutput:
        """Build a compile response from a :class:`CompileResult`."""
        return cls(
            compiled=Compiled(sql=compiled.sql, dialect=compiled.dialect),
            metadata=QueryMetadata.from_compile_result(compiled),
        )
