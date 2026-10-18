"""Core capability response models (SPEC-E7-E8 §2.2).

These are the transport-neutral results the core returns; MCP and CLI adapters
serialize them verbatim. ``QueryResult`` merges the E5 compiler metadata and the
E2 ``ResultSet`` into the single object an agent answer depends on (§2.2), with no
field renamed by any surface.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict

from canonic.compiler.compose import zero_denominator_warning
from canonic.compiler.result import PROBE_ZERO_DENOMINATOR
from canonic.connectors.base import (
    ResultSet,  # noqa: TC001 — Pydantic resolves field annotations at runtime
)
from canonic.contract import CONTRACT_SCHEMA
from canonic.contracts.models import (
    Example,  # noqa: TC001 — Pydantic resolves field annotations at runtime
)
from canonic.contracts.principal import (
    TenantScope,  # noqa: TC001 — Pydantic resolves field annotations at runtime
)
from canonic.trust.periods import open_periods
from canonic.trust.scorer import trust_for_compiled

if TYPE_CHECKING:
    from datetime import datetime

    from canonic.compiler.result import CompileResult, CompositionMetadata
    from canonic.contracts.resolver import Binding
    from canonic.core.probes import ProbeValues
    from canonic.feedback.assertion_history import AssertionHistory
    from canonic.feedback.history import BindingOutcomeHistory

__all__ = [
    "Compiled",
    "CompileOutput",
    "CompositionComponentOut",
    "CompositionOut",
    "CumulativeOut",
    "DimensionInfo",
    "DomainGroup",
    "FinalityOut",
    "BindingOut",
    "ComponentsOut",
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
    "ReportNarrative",
    "ReportRunResult",
    "ReportSectionResult",
    "ReportSummary",
    "ScopeOut",
    "SourceFreshnessOut",
    "TrustScoreOut",
]


class FiredGuardrailOut(BaseModel):
    """A guardrail that fired during compilation (mirrors compiler ``FiredGuardrail``).

    ``predicate`` is the filter or constraint it applied. ``knowledge_refs`` are the ids of the
    knowledge pages that explain it, readable with ``read_knowledge_page``.
    """

    model_config = ConfigDict(frozen=True)

    id: str
    kind: str
    severity: str = "error"
    predicate: str | None = None
    knowledge_refs: list[str] = []


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


class CompositionComponentOut(BaseModel):
    """One side of a composite metric: its binding and the population it counts."""

    model_config = ConfigDict(frozen=True)

    metric: str
    resolved: str | None = None
    population_filter: str | None = None


class CompositionOut(BaseModel):
    """How one composite metric (``ratio``, ``weighted_avg``) was formed for this query.

    ``on_zero_denominator`` is the policy applied to a row with a zero denominator. Under
    ``zero`` that row becomes ``0`` without any warning, so a caller needs it here to tell a
    real zero from a substituted one. ``zero_denominator_rows`` is how many rows of the returned
    result had a zero denominator and were given the policy's value. It is ``None`` when it was
    not counted: without an executed result, and under ``on_zero_denominator: error``.
    """

    model_config = ConfigDict(frozen=True)

    kind: str
    numerator: CompositionComponentOut
    denominator: CompositionComponentOut
    on_zero_denominator: str
    zero_denominator_rows: int | None = None


class CumulativeOut(BaseModel):
    """How a cumulative metric's running total was formed for this query.

    ``partition_by`` is every requested dimension that is not an order dimension, so it
    differs per query. Under ``history: exclude`` the total starts at zero at the first row
    that passes the query's filters. Under ``include`` it runs from the first row of data, and
    ``visibility_filters`` only chose which order tuples are shown.
    """

    model_config = ConfigDict(frozen=True)

    metric: str
    order_by: list[str]
    partition_by: list[str] = []
    on_gap: str
    history: str
    visibility_filters: list[str] = []


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


class ScopeOut(BaseModel):
    """The ``metadata.scope`` block: tenant/role scoping (SPEC-E12 §3 stage 8, S16 AC3)."""

    model_config = ConfigDict(frozen=True)

    #: One tenant as the bare value, several as a list (see ``Principal.tenant_scope``).
    tenant: TenantScope | None = None
    scoped_sources: list[str] = []
    shared_sources: list[str] = []
    roles: list[str] = []
    tenancy_exempt: bool = False


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


class ComponentsOut(BaseModel):
    """The building blocks of a composite metric (``ratio``, ``weighted_avg``)."""

    model_config = ConfigDict(frozen=True)

    numerator: str
    denominator: str
    on_zero_denominator: str

    @classmethod
    def from_binding(cls, binding: Binding) -> ComponentsOut | None:
        """The components of a composite binding, ``None`` for any other kind."""
        if binding.components is None:
            return None
        return cls(
            numerator=binding.components.numerator.metric,
            denominator=binding.components.denominator.metric,
            on_zero_denominator=str(binding.components.on_zero_denominator),
        )


class BindingOut(BaseModel):
    """How a metric is built: the payload of ``resolve_metric`` (SPEC §4.1).

    ``source`` and ``measure`` are ``None`` for a composite, which has no single pair.
    ``components`` names the numerator and denominator metrics instead.
    """

    model_config = ConfigDict(frozen=True)

    metric: str
    kind: str
    source: str | None
    measure: str | None
    components: ComponentsOut | None = None
    population_filter: str | None = None

    @classmethod
    def from_binding(cls, binding: Binding) -> BindingOut:
        return cls(
            metric=binding.metric,
            kind=str(binding.kind),
            source=binding.source,
            measure=binding.measure,
            components=ComponentsOut.from_binding(binding),
            population_filter=binding.binding.canonical.population_filter,
        )


class MetricDetail(BaseModel):
    """Grain, dimensions, owning source, and freshness for one metric (SPEC §4.1).

    ``kind``, ``components`` and ``population_filter`` say how the metric is built, as in
    :class:`BindingOut`. For a composite, ``measures`` lists the measures its components read
    and ``grain`` is the shared grain when both components sit on one source, otherwise empty.
    """

    model_config = ConfigDict(frozen=True)

    metric: str
    kind: str = "single"
    source: str | None
    measure: str | None = None
    components: ComponentsOut | None = None
    population_filter: str | None = None
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
    scope: ScopeOut | None = None
    cumulative: CumulativeOut | None = None
    composition: dict[str, CompositionOut] = {}

    @classmethod
    def from_compile_result(
        cls,
        compiled: CompileResult,
        result: ResultSet | None = None,
        *,
        outcome_history: BindingOutcomeHistory | None = None,
        outcome_window_days: int = 90,
        assertion_history: AssertionHistory | None = None,
        now: datetime | None = None,
        probes: ProbeValues | None = None,
    ) -> QueryMetadata:
        """Project a :class:`CompileResult` onto the §2.2 metadata shape.

        When ``result`` is provided and the compile result carries finality metadata,
        the ``is_final`` column in the result set is used to tally ``final_rows`` and
        ``provisional_rows``. Those tallies also feed the trust tier (SPEC-E14 §6):
        static signals (provenance, assertion coverage) apply regardless of ``result``,
        while the finality/freshness signals only activate once row-level data is known.
        ``outcome_history`` folds in E11's dynamic outcome-history signal (SPEC-E11 §5);
        ``assertion_history`` folds in E16 Phase 2's persisted assertion signal
        (SPEC-E14 §5); omitting either leaves trust scoring as it was before that signal.
        ``now`` is the serving clock: with it, a latest period of a time dimension that has not
        ended is named in ``warnings`` and caps the trust tier at ``provisional``. ``probes`` are
        the counts read from the compiler's probe columns, see :mod:`canonic.core.probes`.
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
        trust = trust_for_compiled(
            compiled,
            result,
            outcome_history=outcome_history,
            outcome_window_days=outcome_window_days,
            assertion_history=assertion_history,
            now=now,
            probes=probes,
        )
        return cls(
            resolved={"metrics": dict(compiled.resolved)},
            guardrails_fired=[
                FiredGuardrailOut(
                    id=g.id,
                    kind=g.kind,
                    severity=g.severity,
                    predicate=g.predicate,
                    knowledge_refs=list(g.knowledge_refs),
                )
                for g in compiled.guardrails_fired
            ],
            freshness=[
                SourceFreshnessOut(
                    source=f.source, last_validated_at=f.last_validated_at, stale=f.stale
                )
                for f in compiled.freshness
            ],
            warnings=[
                *_with_zero_denominator_counts(compiled, probes),
                *(period.warning for period in open_periods(compiled, result, now, probes)),
            ],
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
            scope=(
                ScopeOut(
                    tenant=compiled.scope.tenant,
                    scoped_sources=list(compiled.scope.scoped_sources),
                    shared_sources=list(compiled.scope.shared_sources),
                    roles=list(compiled.scope.roles),
                    tenancy_exempt=compiled.scope.tenancy_exempt,
                )
                if compiled.scope is not None
                else None
            ),
            cumulative=(
                CumulativeOut(
                    metric=compiled.cumulative.metric,
                    order_by=list(compiled.cumulative.order_by),
                    partition_by=list(compiled.cumulative.partition_by),
                    on_gap=compiled.cumulative.on_gap,
                    history=compiled.cumulative.history,
                    visibility_filters=list(compiled.cumulative.visibility_filters),
                )
                if compiled.cumulative is not None
                else None
            ),
            composition={
                name: _composition_out(
                    composition, (probes or {}).get((PROBE_ZERO_DENOMINATOR, name))
                )
                for name, composition in compiled.compositions.items()
            },
        )


def _with_zero_denominator_counts(compiled: CompileResult, probes: ProbeValues | None) -> list[str]:
    """``compiled.warnings`` with each zero-denominator warning set to what the result shows.

    The compiler can only state the policy. Once the rows are counted, a ratio with no zero
    denominator in the result needs no warning, and one with some names how many. Without a
    count (``compile`` has no result) the generic warning stays.
    """
    replaced: dict[str, list[str]] = {}
    for name, metric in compiled.zero_denominator_metrics.items():
        rows = (probes or {}).get((PROBE_ZERO_DENOMINATOR, name))
        if rows is None:
            continue
        replaced[zero_denominator_warning(metric)] = (
            [zero_denominator_warning(metric, rows)] if rows else []
        )
    out: list[str] = []
    for warning in compiled.warnings:
        out.extend(replaced.get(warning, [warning]))
    return out


def _composition_out(
    composition: CompositionMetadata, zero_denominator_rows: int | None
) -> CompositionOut:
    numerator, denominator = (
        CompositionComponentOut(
            metric=c.metric, resolved=c.resolved, population_filter=c.population_filter
        )
        for c in composition.components
    )
    return CompositionOut(
        kind=composition.kind,
        numerator=numerator,
        denominator=denominator,
        on_zero_denominator=str(composition.on_zero_denominator),
        zero_denominator_rows=zero_denominator_rows,
    )


class QueryResult(BaseModel):
    """The combined ``query`` response: ``result`` + ``compiled`` + ``metadata`` (SPEC §2.2)."""

    model_config = ConfigDict(frozen=True)

    result: ResultSet
    compiled: Compiled
    metadata: QueryMetadata

    @classmethod
    def from_parts(
        cls,
        compiled: CompileResult,
        result: ResultSet,
        *,
        outcome_history: BindingOutcomeHistory | None = None,
        outcome_window_days: int = 90,
        assertion_history: AssertionHistory | None = None,
        now: datetime | None = None,
        probes: ProbeValues | None = None,
    ) -> QueryResult:
        """Merge the compiler output and the executed result set (no field renamed)."""
        return cls(
            result=result,
            compiled=Compiled(sql=compiled.sql, dialect=compiled.dialect),
            metadata=QueryMetadata.from_compile_result(
                compiled,
                result=result,
                outcome_history=outcome_history,
                outcome_window_days=outcome_window_days,
                assertion_history=assertion_history,
                now=now,
                probes=probes,
            ),
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


class ReportSummary(BaseModel):
    """One committed report's directory-listing entry, as returned by ``list_reports``."""

    model_config = ConfigDict(frozen=True)

    id: str
    title: str
    description: str | None = None
    owner: str | None = None
    domain: str | None = None


class ReportNarrative(BaseModel):
    """A rendered knowledge page attached to a report section (``narrative_from``).

    ``body`` is always the **rendered** page (live ``{{ sl:entity.expr }}`` definitions
    resolved to SQL), never the raw template — same rule as ``read_page`` (E6 §7).
    """

    model_config = ConfigDict(frozen=True)

    page_id: str
    body: str
    meta: dict[str, Any] = {}


class ReportSectionResult(BaseModel):
    """One section's outcome within a ``run_report`` response.

    Exactly one of ``result``/``error`` is set: a failing section resolves to a
    structured ``{code, message, candidates?}`` error (the same wire shape every other
    capability returns) rather than aborting the whole report (S17).
    """

    model_config = ConfigDict(frozen=True)

    title: str
    result: QueryResult | None = None
    narrative: ReportNarrative | None = None
    error: dict[str, Any] | None = None


class ReportRunResult(BaseModel):
    """The ``run_report`` response: ordered section outcomes (S16)."""

    model_config = ConfigDict(frozen=True)

    report_id: str
    sections: list[ReportSectionResult]
