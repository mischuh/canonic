"""Opaque compile path (grain-locked pre-computed values, SPEC §4.4)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlglot import exp

from canonic.compiler.compose import LeafRef, MetricLeaves, MetricPlan
from canonic.compiler.leaf import AuxCte, LeafContext, LeafInputs, LeafMetric, plan_leaf
from canonic.compiler.result import OpaqueMetadata
from canonic.exc import Unresolved, UnsupportedMeasure

if TYPE_CHECKING:
    from collections.abc import Sequence

    from canonic.compiler._helpers import DimMask
    from canonic.compiler.joins import JoinEdge
    from canonic.compiler.query import SemanticQuery
    from canonic.contracts.principal import EffectivePolicy, Principal
    from canonic.contracts.resolver import Binding as ResolverBinding
    from canonic.contracts.resolver import ContractResolver
    from canonic.semantic.models import Dimension, SemanticSource

from canonic.compiler._helpers import (
    _alias,
    _dimension_expr,
    _dimension_output_names,
    _find_measure,
    _from_and_joins,
    _measure_expr,
    _ResolvedMetric,
)


def plan_metric(
    query: SemanticQuery,
    queried_name: str,
    binding: ResolverBinding,
    resolver: ContractResolver,
    sources_by_name: dict[str, SemanticSource],
    *,
    principal: Principal,
    effective_policy: EffectivePolicy,
) -> MetricLeaves:
    """Plan an opaque metric as one leaf — served at native grain only, never re-aggregated (§4.4).

    The grain lock is the whole point: a pre-computed value has no formula the compiler
    could re-apply at a coarser grain, so anything other than an exact match on
    ``native_grain`` is refused rather than summed. That refusal fails the whole query,
    even when every other requested metric could have been served (S15 AC3) — a result
    missing one of its columns is not the result that was asked for.
    """
    assert binding.opaque is not None  # noqa: S101 — routing guarantees this kind
    opaque = binding.opaque
    assert binding.source is not None and binding.measure is not None  # noqa: S101
    source_name = binding.source

    source = sources_by_name.get(source_name)
    if source is None:
        raise Unresolved(f"metric {queried_name!r} binds to unknown source {source_name!r}")
    measure = _find_measure(source, binding.measure)
    if measure is None:
        raise Unresolved(
            f"metric {queried_name!r} binds to unknown measure {source_name}.{binding.measure!r}"
        )

    def build(
        inputs: LeafInputs, leaf_metrics: Sequence[LeafMetric]
    ) -> tuple[exp.Expression, tuple[AuxCte, ...]]:
        requested_dims = {dim.name for _, dim in inputs.dimensions}
        if requested_dims != set(opaque.native_grain):
            native_repr = " × ".join(sorted(opaque.native_grain))
            raise UnsupportedMeasure(
                f"metric {queried_name!r} is opaque and can only be served at its native "
                f"grain ({native_repr}); cannot re-aggregate a pre-computed value — "
                f"requested grain was {sorted(requested_dims)!r}"
            )
        return (
            _build_opaque(
                owner=inputs.owner,
                metric=leaf_metrics[0].resolved,
                alias=queried_name,
                dimensions=inputs.dimensions,
                where_conditions=inputs.where_conditions,
                join_edges=inputs.join_edges,
                sources_by_name=sources_by_name,
                dim_mask=inputs.dim_mask,
            ),
            (),
        )

    leaf = plan_leaf(
        LeafContext(
            query=query,
            resolver=resolver,
            sources_by_name=sources_by_name,
            principal=principal,
            effective_policy=effective_policy,
        ),
        source_name,
        [
            LeafMetric(
                resolved=_ResolvedMetric(name=queried_name, source=source_name, measure=measure),
                population_filter=binding.binding.canonical.population_filter,
                alias=queried_name,
            )
        ],
        strategy="opaque",
        strategy_params=(("native_grain", ",".join(sorted(opaque.native_grain))),),
        builder=build,
        fusable=False,
    )

    resolved_key = binding.resolved_key
    assert resolved_key is not None  # noqa: S101 — source/measure asserted non-None above

    return MetricLeaves(
        leaves=[leaf],
        metric=MetricPlan(name=queried_name, refs=(LeafRef(leaf=0, column=queried_name),)),
        resolved=resolved_key,
        opaque=OpaqueMetadata(
            source=source_name,
            measure=binding.measure,
            native_grain=list(opaque.native_grain),
        ),
    )


def _build_opaque(
    owner: str,
    metric: _ResolvedMetric,
    alias: str,
    dimensions: list[tuple[str, Dimension]],
    where_conditions: list[exp.Expression],
    join_edges: list[JoinEdge],
    sources_by_name: dict[str, SemanticSource],
    dim_mask: DimMask | None = None,
) -> exp.Select:
    """Build a raw direct-lookup SELECT for an opaque metric — no aggregate, no GROUP BY (§4.4)."""
    select = exp.Select()
    projections: list[exp.Expression] = []
    mask = dim_mask or {}
    for (src, dim), name in zip(dimensions, _dimension_output_names(dimensions), strict=True):
        projections.append(_alias(_dimension_expr(src, dim, mask.get((src, dim.column))), name))
    projections.append(_alias(_measure_expr(metric.source, metric.measure), alias))
    select = select.select(*projections)
    select = _from_and_joins(select, owner, join_edges, sources_by_name)
    if where_conditions:
        select = select.where(exp.and_(*where_conditions))
    return select
