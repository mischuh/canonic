"""Opaque compile path (grain-locked pre-computed values, SPEC §4.4)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlglot import exp

from canonic.compiler.dialect import adapter_for
from canonic.compiler.joins import JoinEdge, build_alias_tree, plan_joins
from canonic.compiler.result import (
    CompileResult,
    OpaqueMetadata,
)
from canonic.exc import Unresolved, UnsupportedMeasure

if TYPE_CHECKING:
    from canonic.compiler.query import SemanticQuery
    from canonic.contracts.resolver import Binding as ResolverBinding
    from canonic.contracts.resolver import ContractResolver
    from canonic.semantic.models import Dimension, SemanticSource

from canonic.compiler._helpers import (
    _alias,
    _bind_filters,
    _dimension_expr,
    _dimension_output_names,
    _enforce_guardrails,
    _find_measure,
    _freshness,
    _from_and_joins,
    _measure_expr,
    _population_filter_conditions,
    _resolve_dimensions,
    _ResolvedMetric,
)


def _compile_opaque(
    query: SemanticQuery,
    binding: ResolverBinding,
    resolver: ContractResolver,
    sources_by_name: dict[str, SemanticSource],
    *,
    dialect: str = "postgres",
) -> CompileResult:
    """Compile an opaque metric — serve at native grain only, no re-aggregation (§4.4).

    At native grain (requested dimensions == native_grain set) → direct lookup with no GROUP BY.
    Any other grain → UNSUPPORTED_MEASURE with a rationale.
    """
    assert binding.opaque is not None  # noqa: S101 — routing guarantees this kind
    opaque = binding.opaque
    adapter = adapter_for(dialect)
    queried_name = query.metrics[0]

    assert binding.source is not None and binding.measure is not None  # noqa: S101 — enforced by model_validator
    source_name = binding.source
    alias_to_source = build_alias_tree(source_name, sources_by_name)

    # Stage 2 — dimensions & filters.
    dimensions = _resolve_dimensions(query, sources_by_name, source_name, alias_to_source)
    where_conditions, filter_sources = _bind_filters(
        query.filters, sources_by_name, source_name, alias_to_source
    )
    referenced = {alias for alias, _ in dimensions} | filter_sources | {source_name}

    # Stage 3 — join graph.
    join_edges = plan_joins(
        source_name, referenced - {source_name}, sources_by_name, via=list(query.via) or None
    )

    # Grain guard (AC1): requested dimensions must exactly match native_grain (§4.4).
    requested_dims = {dim.name for _, dim in dimensions}
    native_dims = set(opaque.native_grain)
    if requested_dims != native_dims:
        native_repr = " × ".join(sorted(opaque.native_grain))
        raise UnsupportedMeasure(
            f"metric {queried_name!r} is opaque and can only be served at its native grain "
            f"({native_repr}); cannot re-aggregate a pre-computed value — "
            f"requested grain was {sorted(requested_dims)!r}"
        )

    # Resolve the measure object for guardrails and SQL emission.
    source = sources_by_name.get(source_name)
    if source is None:
        raise Unresolved(f"metric {queried_name!r} binds to unknown source {source_name!r}")
    measure = _find_measure(source, binding.measure)
    if measure is None:
        raise Unresolved(
            f"metric {queried_name!r} binds to unknown measure {source_name}.{binding.measure!r}"
        )
    resolved_metric = _ResolvedMetric(name=queried_name, source=source_name, measure=measure)

    # population_filter — defines the population this metric is about (§4.5); before guardrails.
    where_conditions += _population_filter_conditions(
        binding.binding.canonical.population_filter, sources_by_name, source_name, alias_to_source
    )

    # Stage 6 — guardrails.
    guard_conditions, fired = _enforce_guardrails(
        [resolved_metric], resolver, query.context, sources_by_name
    )
    where_conditions += guard_conditions

    # Stage 7 — emit raw lookup (no aggregate, no GROUP BY).
    ast = _build_opaque(
        owner=source_name,
        metric=resolved_metric,
        dimensions=dimensions,
        where_conditions=where_conditions,
        join_edges=join_edges,
        sources_by_name=sources_by_name,
    )
    sql = adapter.emit(ast, limit=query.limit)

    # Stage 8 — result metadata.
    used_sources = sorted({source_name} | {e.join.to for e in join_edges})
    return CompileResult(
        sql=sql,
        dialect=adapter.dialect,
        resolved={queried_name: f"opaque({source_name}.{binding.measure})"},
        guardrails_fired=fired,
        freshness=[_freshness(sources_by_name[s]) for s in used_sources],
        warnings=[],
        opaque=OpaqueMetadata(
            source=source_name,
            measure=binding.measure,
            native_grain=list(opaque.native_grain),
        ),
    )


def _build_opaque(
    owner: str,
    metric: _ResolvedMetric,
    dimensions: list[tuple[str, Dimension]],
    where_conditions: list[exp.Expression],
    join_edges: list[JoinEdge],
    sources_by_name: dict[str, SemanticSource],
) -> exp.Select:
    """Build a raw direct-lookup SELECT for an opaque metric — no aggregate, no GROUP BY (§4.4)."""
    select = exp.Select()
    projections: list[exp.Expression] = []
    for (src, dim), name in zip(dimensions, _dimension_output_names(dimensions), strict=True):
        projections.append(_alias(_dimension_expr(src, dim), name))
    projections.append(_alias(_measure_expr(metric.source, metric.measure), metric.measure.name))
    select = select.select(*projections)
    select = _from_and_joins(select, owner, join_edges, sources_by_name)
    if where_conditions:
        select = select.where(exp.and_(*where_conditions))
    return select
