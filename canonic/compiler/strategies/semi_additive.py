"""Semi-additive compile path (partial_additive, SPEC §4.2)."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from sqlglot import exp

from canonic.compiler.compose import LeafRef, MetricLeaves, MetricPlan
from canonic.compiler.joins import JoinEdge, build_alias_tree
from canonic.compiler.leaf import AuxCte, LeafContext, LeafInputs, LeafMetric, plan_leaf
from canonic.compiler.result import PartialAdditiveMetadata
from canonic.contracts.models import CollapseAgg
from canonic.exc import FanoutUnsafe, Unresolved, UnsupportedMeasure
from canonic.semantic.models import Additivity, Measure

if TYPE_CHECKING:
    from collections.abc import Sequence

    from canonic.compiler.query import SemanticQuery
    from canonic.contracts.principal import EffectivePolicy, Principal
    from canonic.contracts.resolver import Binding as ResolverBinding
    from canonic.contracts.resolver import ContractResolver
    from canonic.semantic.models import Dimension, SemanticSource

from canonic.compiler._helpers import (
    _alias,
    _build_simple,
    _dimension_expr,
    _dimension_output_names,
    _find_dimension,
    _find_measure,
    _from_and_joins,
    _func,
    _input_columns,
    _measure_expr,
    _parse,
    _qualify_to,
    _resolve_dimensions,
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
    """Plan a semi_additive metric as one leaf (SPEC §4.2).

    The collapse happens *inside* the leaf: by the time compose sees it, the metric is an
    ordinary column, so a semi-additive metric combines with any other metric without the
    compose step needing to know anything about windows or snapshots.

    Which shape the leaf takes hinges on whether the query groups by ``collapse_dimension``:
    grouped by it the measure is plainly additive, and collapsing across it needs
    ``collapse_agg`` applied to one row per entity per snapshot.

    Finality (stage 5) remains deferred for semi_additive, as before.
    """
    assert binding.semi_additive is not None  # noqa: S101 — routing guarantees semi_additive kind
    sa = binding.semi_additive
    assert binding.source is not None and binding.measure is not None  # noqa: S101
    source_name = binding.source
    alias_to_source = build_alias_tree(source_name, sources_by_name)

    source_obj = sources_by_name.get(source_name)
    if source_obj is None:
        raise Unresolved(f"metric {queried_name!r} binds to unknown source {source_name!r}")
    measure_obj = _find_measure(source_obj, binding.measure)
    if measure_obj is None:
        raise Unresolved(
            f"metric {queried_name!r} binds to unknown measure {source_name}.{binding.measure!r}"
        )
    if measure_obj.additivity is not Additivity.ADDITIVE:
        raise UnsupportedMeasure(
            f"semi_additive binding {queried_name!r}: base measure "
            f"{source_name}.{measure_obj.name!r} must be additive"
        )
    if not measure_obj.is_p0_compilable:
        raise UnsupportedMeasure(
            f"measure {source_name}.{measure_obj.name!r} uses an aggregate function "
            f"not supported at P0"
        )

    collapse = _find_dimension(sa.collapse_dimension, sources_by_name, source_name, alias_to_source)
    if collapse is None:
        raise Unresolved(
            f"semi_additive binding {queried_name!r}: collapse_dimension "
            f"{sa.collapse_dimension!r} is not declared on any source"
        )

    # The window partitions by the source's own grain minus the collapse dimension, not by
    # the requested output dimensions: those may be a strict subset of the entity key, and
    # a scalar query still has to dedupe per entity before summing.
    grain_dims: list[tuple[str, Dimension]] = []
    for grain_col in source_obj.grain:
        if grain_col == sa.collapse_dimension:
            continue
        grain_dim = _find_dimension(grain_col, sources_by_name, source_name, alias_to_source)
        if grain_dim is None:
            raise Unresolved(
                f"semi_additive binding {queried_name!r}: grain column {grain_col!r} of "
                f"source {source_name!r} is not declared as a dimension"
            )
        grain_dims.append(grain_dim)

    # Which branch this leaf takes decides its output column name, and the column name has
    # to be known before the leaf is planned. Resolving the dimensions twice is cheap and
    # pure; what it buys is preserving the existing (inconsistent) aliasing exactly —
    # collapsing across the dimension names the column after the metric, grouping by it
    # names the column after the measure. Unifying the two is a separate, user-visible
    # change and is deliberately not made here.
    grouped = {
        dim.name
        for _alias, dim in _resolve_dimensions(query, sources_by_name, source_name, alias_to_source)
    }
    collapsed = sa.collapse_dimension not in grouped
    column = queried_name if collapsed else measure_obj.name

    def build(
        inputs: LeafInputs, leaf_metrics: Sequence[LeafMetric]
    ) -> tuple[exp.Expression, tuple[AuxCte, ...]]:
        if inputs.fanout:
            raise FanoutUnsafe(
                f"semi_additive metric {queried_name!r} cannot be used with a "
                f"one_to_many/many_to_many join; request it without the fanning dimension"
            )
        if not collapsed:
            return (
                _build_simple(
                    inputs.owner,
                    [lm.resolved for lm in leaf_metrics],
                    inputs.dimensions,
                    inputs.where_conditions,
                    inputs.join_edges,
                    sources_by_name,
                    measure_aliases=[column],
                ),
                (),
            )
        return _build_semi_additive(
            owner=inputs.owner,
            measure=measure_obj,
            metric_name=column,
            collapse_alias=collapse[0],
            collapse_dim=collapse[1],
            dimensions=inputs.dimensions,
            grain_dims=grain_dims,
            where_conditions=inputs.where_conditions,
            join_edges=inputs.join_edges,
            sources_by_name=sources_by_name,
            collapse_agg=sa.collapse_agg,
            name_prefix=inputs.name_prefix,
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
                resolved=_ResolvedMetric(
                    name=queried_name, source=source_name, measure=measure_obj
                ),
                population_filter=binding.binding.canonical.population_filter,
                alias=column,
            )
        ],
        strategy="semi_additive",
        strategy_params=(
            ("collapse_dimension", sa.collapse_dimension),
            ("collapse_agg", str(sa.collapse_agg)),
        ),
        builder=build,
        fusable=False,
    )

    return MetricLeaves(
        leaves=[leaf],
        metric=MetricPlan(name=column, refs=(LeafRef(leaf=0, column=column),)),
        resolved=f"{source_name}.{measure_obj.name}",
        partial_additive=PartialAdditiveMetadata(
            kind="semi_additive",
            collapse_dimension=sa.collapse_dimension,
            collapse_agg=str(sa.collapse_agg),
            collapsed=collapsed,
        ),
    )


def _build_semi_additive(
    owner: str,
    measure: Measure,
    metric_name: str,
    collapse_alias: str,
    collapse_dim: Dimension,
    dimensions: list[tuple[str, Dimension]],
    grain_dims: list[tuple[str, Dimension]],
    where_conditions: list[exp.Expression],
    join_edges: list[JoinEdge],
    sources_by_name: dict[str, SemanticSource],
    collapse_agg: CollapseAgg,
    name_prefix: str = "",
) -> tuple[exp.Expression, tuple[AuxCte, ...]]:
    """Emit the window or nested-aggregate SQL for a semi_additive collapse (SPEC §4.2).

    ``last``/``first`` → ROW_NUMBER() window CTE then filter rn = 1.
    ``avg``/``min``/``max`` → per_snapshot CTE with two-level GROUP BY.

    The inner CTE is returned rather than attached, so compose can declare it alongside
    this leaf in the one outer ``WITH`` instead of nesting a ``WITH`` inside a CTE body
    (AMENDMENT §3.5). With no prefix the names are unchanged, which is what keeps a
    single-metric query byte-identical to what it compiled to before.
    """
    collapse_col = exp.column(collapse_dim.column, table=collapse_alias)

    if collapse_agg in {CollapseAgg.LAST, CollapseAgg.FIRST}:
        order_dir = "DESC" if collapse_agg is CollapseAgg.LAST else "ASC"

        # Inner CTE: project grouped dimensions + raw input columns + ROW_NUMBER window.
        # The window partitions by the source's grain (minus collapse_dimension), not by
        # the requested output dimensions — those may be a strict subset (or unrelated,
        # via a join) of the entity key needed to dedupe "last per entity" correctly.
        dim_names = _dimension_output_names(dimensions)
        inner = exp.Select()
        inner_projections: list[exp.Expression] = []
        seen_names: set[str] = set()
        for (src, dim), name in zip(dimensions, dim_names, strict=True):
            expr = _dimension_expr(src, dim)
            inner_projections.append(_alias(expr, name))
            seen_names.add(name)

        partition_exprs: list[exp.Expression] = []
        grain_names = _dimension_output_names(grain_dims)
        for (src, dim), name in zip(grain_dims, grain_names, strict=True):
            expr = _dimension_expr(src, dim)
            partition_exprs.append(expr)
            if name not in seen_names:
                inner_projections.append(_alias(expr, name))
                seen_names.add(name)

        for input_col in _input_columns(measure):
            inner_projections.append(_alias(exp.column(input_col, table=owner), input_col))

        order_item = cast(
            "exp.Expression",
            exp.Ordered(
                this=collapse_col,
                desc=order_dir == "DESC",
            ),
        )
        window_spec = cast(
            "exp.Expression",
            exp.Window(
                this=exp.RowNumber(),
                partition_by=partition_exprs,
                order=exp.Order(expressions=[order_item]),
            ),
        )
        inner_projections.append(_alias(window_spec, "rn"))
        inner = inner.select(*inner_projections)
        inner = _from_and_joins(inner, owner, join_edges, sources_by_name)
        if where_conditions:
            inner = inner.where(exp.and_(*where_conditions))

        # Outer SELECT: aggregate measure over ranked rows, filter rn = 1.
        _RANKED = f"{name_prefix}ranked"
        outer = exp.Select()
        outer_projections: list[exp.Expression] = []
        outer_group: list[exp.Expression] = []
        for name in dim_names:
            dim_col = cast("exp.Expression", exp.column(name, table=_RANKED))
            outer_projections.append(_alias(dim_col, name))
            outer_group.append(dim_col)
        outer_projections.append(_alias(_qualify_to(_parse(measure.expr), _RANKED), metric_name))
        outer = outer.select(*outer_projections)
        outer = outer.from_(exp.to_table(_RANKED))
        rn_filter = cast(
            "exp.Expression",
            exp.EQ(
                this=cast("exp.Expression", exp.column("rn", table=_RANKED)),
                expression=exp.Literal.number(1),
            ),
        )
        outer = outer.where(rn_filter)
        if outer_group:
            outer = outer.group_by(*outer_group)

        return cast("exp.Expression", outer), (AuxCte(name=_RANKED, body=inner),)

    # avg / min / max — nested GROUP BY form.
    _PER_SNAP = f"{name_prefix}per_snapshot"
    agg_fn = str(collapse_agg).upper()

    # Inner CTE: group by (grouped dims + collapse dim), compute measure per snapshot.
    dim_names = _dimension_output_names(dimensions)
    inner = exp.Select()
    inner_projections = []
    inner_group: list[exp.Expression] = []
    for (src, dim), name in zip(dimensions, dim_names, strict=True):
        expr = _dimension_expr(src, dim)
        inner_projections.append(_alias(expr, name))
        inner_group.append(expr)
    inner_projections.append(_alias(_measure_expr(owner, measure), "m"))
    inner_group.append(collapse_col)
    inner = inner.select(*inner_projections)
    inner = _from_and_joins(inner, owner, join_edges, sources_by_name)
    if where_conditions:
        inner = inner.where(exp.and_(*where_conditions))
    inner = inner.group_by(*inner_group)

    # Outer SELECT: apply agg_fn over the per-snapshot measure.
    outer = exp.Select()
    outer_projections = []
    outer_group = []
    for name in dim_names:
        dim_col = cast("exp.Expression", exp.column(name, table=_PER_SNAP))
        outer_projections.append(_alias(dim_col, name))
        outer_group.append(dim_col)
    m_col = cast("exp.Expression", exp.column("m", table=_PER_SNAP))
    outer_projections.append(_alias(_func(agg_fn, m_col), metric_name))
    outer = outer.select(*outer_projections)
    outer = outer.from_(exp.to_table(_PER_SNAP))
    if outer_group:
        outer = outer.group_by(*outer_group)

    return cast("exp.Expression", outer), (AuxCte(name=_PER_SNAP, body=inner),)
