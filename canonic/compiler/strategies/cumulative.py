"""Cumulative compile path: running totals along fixed order dimensions.

The base measure is aggregated to the requested dimensions in an ordinary leaf, so joins,
fanout, tenancy, ``population_filter`` and guardrails apply exactly as for a ``single``
metric. The running total itself is a window that compose puts over the spine, after every
leaf is aggregated. Nothing is ever accumulated over raw rows.

Query filters are split by the columns they read. A filter that reads no order column
narrows the leaf like any other filter, since partitions accumulate independently. A filter
that reads only order columns decides which order tuples are shown and is kept out of the
leaf, so a result from March on still carries the total since the first row of data. A
filter that reads both has no single answer to "which rows are accumulated" and is refused.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlglot import exp

from canonic.compiler._helpers import (
    _alias,
    _bind_filters,
    _dim_mask_strategy,
    _dimension_expr,
    _dimension_output_names,
    _find_dimension,
    _from_and_joins,
    _resolve_dimensions,
)
from canonic.compiler.compose import VISIBLE_CTE, Accumulate, LeafRef, MetricLeaves, MetricPlan
from canonic.compiler.joins import build_alias_tree
from canonic.compiler.leaf import AuxCte, LeafContext, LeafMetric, _build_additive, plan_leaf
from canonic.compiler.result import CumulativeMetadata
from canonic.compiler.strategies.simple_additive import _bind_metric
from canonic.contracts.models import OnGap
from canonic.exc import Unresolved, UnsupportedMeasure
from canonic.semantic.models import Additivity

if TYPE_CHECKING:
    from collections.abc import Sequence

    from canonic.compiler.leaf import LeafInputs
    from canonic.compiler.query import SemanticQuery
    from canonic.contracts.principal import EffectivePolicy, Principal
    from canonic.contracts.resolver import Binding as ResolverBinding
    from canonic.contracts.resolver import ContractResolver
    from canonic.semantic.models import Dimension, SemanticSource

__all__ = ["plan_metric"]

#: Warning attached to a ``fill_observed`` result that has partitions to fill, since its row
#: count is no longer bounded by the rows that hold data.
_FILL_WARNING = (
    "cumulative metric {metric!r} uses on_gap: fill_observed; the result contains filled "
    "rows that carry the running total forward, up to observed order tuples times partitions"
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
    """Plan a cumulative metric as one additive leaf plus an accumulation step.

    The order dimensions come from the binding, the partition is every other requested
    dimension. Every order dimension must be requested: adding one implicitly would return
    more rows than the caller asked for, which is a silent change of shape.
    """
    assert binding.cumulative is not None  # noqa: S101 — routing guarantees cumulative kind
    cumulative = binding.cumulative
    resolved = _bind_metric(queried_name, binding, sources_by_name)
    owner = resolved.source
    measure = resolved.measure
    if measure.additivity is not Additivity.ADDITIVE or not measure.is_p0_compilable:
        raise UnsupportedMeasure(
            f"cumulative binding {queried_name!r}: base measure {owner}.{measure.name!r} "
            f"must be additive and use sum, count, min or max"
        )

    alias_to_source = build_alias_tree(owner, sources_by_name)
    dimensions = _resolve_dimensions(query, sources_by_name, owner, alias_to_source)
    output_names = _dimension_output_names(dimensions)
    order_dims = _order_dimensions(
        queried_name, cumulative.order_by, dimensions, sources_by_name, owner, alias_to_source
    )
    order_by = tuple(output_names[i] for i in order_dims)
    order_set = set(order_dims)
    partition_by = tuple(name for i, name in enumerate(output_names) if i not in order_set)

    order_columns = {
        (dimensions[i][0], column)
        for i in order_dims
        for column in dimensions[i][1].backing_columns()
    }
    regular, visibility = _split_filters(
        queried_name,
        query.filters,
        order_columns,
        sources_by_name,
        owner,
        alias_to_source,
        effective_policy,
    )
    visibility_conditions, _ = _bind_filters(
        visibility, sources_by_name, owner, alias_to_source, effective_policy
    )

    def build(
        inputs: LeafInputs, leaf_metrics: Sequence[LeafMetric]
    ) -> tuple[exp.Expression, tuple[AuxCte, ...]]:
        select, _ = _build_additive(inputs, leaf_metrics)
        if not visibility_conditions:
            return select, ()
        return select, (
            AuxCte(
                name=f"{inputs.name_prefix}{VISIBLE_CTE}",
                body=_visible_tuples(inputs, order_dims, visibility_conditions),
            ),
        )

    leaf = plan_leaf(
        LeafContext(
            query=query.model_copy(update={"filters": regular}),
            resolver=resolver,
            sources_by_name=sources_by_name,
            principal=principal,
            effective_policy=effective_policy,
        ),
        owner,
        [
            LeafMetric(
                resolved=resolved,
                population_filter=binding.binding.canonical.population_filter,
                alias=queried_name,
            )
        ],
        strategy="cumulative",
        strategy_params=(
            ("order_by", ",".join(order_by)),
            ("visibility", "\x00".join(sorted(visibility))),
        ),
        builder=build,
    )

    fill_observed = cumulative.on_gap is OnGap.FILL_OBSERVED
    return MetricLeaves(
        leaves=[leaf],
        metric=MetricPlan(
            name=queried_name,
            refs=(LeafRef(leaf=0, column=queried_name),),
            accumulate=Accumulate(
                order_by=order_by,
                partition_by=partition_by,
                fill_observed=fill_observed,
                visibility=bool(visibility_conditions),
            ),
        ),
        resolved=binding.resolved_key or f"cumulative({owner}.{measure.name})",
        cumulative=CumulativeMetadata(
            metric=queried_name,
            order_by=list(cumulative.order_by),
            partition_by=[
                query.dimensions[i] for i in range(len(dimensions)) if i not in order_set
            ],
            on_gap=str(cumulative.on_gap),
            visibility_filters=list(visibility),
        ),
        warnings=(
            (_FILL_WARNING.format(metric=queried_name),) if fill_observed and partition_by else ()
        ),
    )


def _order_dimensions(
    metric: str,
    order_by: Sequence[str],
    dimensions: list[tuple[str, Dimension]],
    sources_by_name: dict[str, SemanticSource],
    owner: str,
    alias_to_source: dict[str, str],
) -> list[int]:
    """Index into the requested dimensions of each order dimension, in ``order_by`` order.

    Matching goes through the resolved ``(alias, dimension)`` pair, so a request that names
    an order dimension by an alias or qualified name still counts.
    """
    requested = {(alias, dim.name): i for i, (alias, dim) in enumerate(dimensions)}
    indexes: list[int] = []
    missing: list[str] = []
    for name in order_by:
        found = _find_dimension(name, sources_by_name, owner, alias_to_source)
        if found is None:
            raise Unresolved(
                f"cumulative binding {metric!r}: order_by dimension {name!r} is not "
                f"reachable from source {owner!r}"
            )
        index = requested.get((found[0], found[1].name))
        if index is None:
            missing.append(name)
        else:
            indexes.append(index)
    if missing:
        names = ", ".join(repr(n) for n in missing)
        raise UnsupportedMeasure(
            f"cumulative metric {metric!r} accumulates along {names}; add "
            f"{'it' if len(missing) == 1 else 'them'} to the query's dimensions"
        )
    return indexes


def _split_filters(
    metric: str,
    filters: Sequence[str],
    order_columns: set[tuple[str, str]],
    sources_by_name: dict[str, SemanticSource],
    owner: str,
    alias_to_source: dict[str, str],
    effective_policy: EffectivePolicy,
) -> tuple[list[str], list[str]]:
    """Split query filters into leaf filters and visibility filters.

    Classified on the physical columns a filter reads once bound, not on dimension names,
    so a filter on the raw column behind an order dimension is still recognised.
    """
    regular: list[str] = []
    visibility: list[str] = []
    for raw in filters:
        (bound,), _ = _bind_filters(
            [raw], sources_by_name, owner, alias_to_source, effective_policy
        )
        columns = {(col.table, col.name) for col in bound.find_all(exp.Column)}
        on_order = columns & order_columns
        if not on_order:
            regular.append(raw)
        elif on_order == columns:
            visibility.append(raw)
        else:
            raise UnsupportedMeasure(
                f"cumulative metric {metric!r}: filter {raw!r} reads order and non-order "
                f"columns at once; split it into one filter per kind"
            )
    return regular, visibility


def _visible_tuples(
    inputs: LeafInputs, order_dims: Sequence[int], visibility: Sequence[exp.Expression]
) -> exp.Expression:
    """Distinct order tuples of the leaf's source rows that pass the visibility filters.

    Built over the same joins and WHERE as the leaf, so a coarse bucket is shown when at
    least one of its rows matches the filter, and a tenant or guardrail predicate can never
    make a tuple visible that the leaf itself would not have.
    """
    names = inputs.dim_names
    projections: list[exp.Expression] = []
    for i in order_dims:
        src, dim = inputs.dimensions[i]
        expr = _dimension_expr(src, dim, _dim_mask_strategy(inputs.dim_mask, src, dim))
        projections.append(_alias(expr, names[i]))
    select = exp.Select().select(*projections).distinct()
    select = _from_and_joins(select, inputs.owner, inputs.join_edges, inputs.ctx.sources_by_name)
    conditions = [*inputs.where_conditions, *(c.copy() for c in visibility)]
    return select.where(exp.and_(*conditions))
