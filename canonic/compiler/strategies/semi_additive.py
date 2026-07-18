"""Semi-additive compile path (partial_additive, SPEC §4.2)."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from sqlglot import exp

from canonic.compiler.dialect import adapter_for
from canonic.compiler.joins import JoinEdge, build_alias_tree, plan_joins
from canonic.compiler.result import (
    CompileResult,
    PartialAdditiveMetadata,
)
from canonic.contracts.models import CollapseAgg
from canonic.exc import FanoutUnsafe, Unresolved, UnsupportedMeasure
from canonic.semantic.models import Additivity, Measure

if TYPE_CHECKING:
    from canonic.compiler.query import SemanticQuery
    from canonic.contracts.resolver import Binding as ResolverBinding
    from canonic.contracts.resolver import ContractResolver
    from canonic.semantic.models import Dimension, SemanticSource

from canonic.compiler._helpers import (
    _FANOUT,
    _alias,
    _bind_filters,
    _build_simple,
    _dimension_expr,
    _dimension_output_names,
    _enforce_guardrails,
    _find_dimension,
    _find_measure,
    _freshness,
    _from_and_joins,
    _func,
    _input_columns,
    _measure_expr,
    _parse,
    _population_filter_conditions,
    _qualify_to,
    _resolve_dimensions,
    _ResolvedMetric,
)


def _compile_semi_additive(
    query: SemanticQuery,
    binding: ResolverBinding,
    resolver: ContractResolver,
    sources_by_name: dict[str, SemanticSource],
    *,
    dialect: str = "postgres",
) -> CompileResult:
    """Compile a semi_additive metric via window or nested-aggregate collapse (SPEC §4.2).

    The decision hinges on whether the query groups by ``collapse_dimension``:
    - Grouped by it → additive; plain sum via ``_build_simple``.
    - Not grouped (collapsing across it) → ``_build_semi_additive`` applies collapse_agg.

    Finality (stage 5) is deferred for semi_additive in this release (P1 scope).
    """
    assert binding.semi_additive is not None  # noqa: S101 — routing guarantees semi_additive kind
    sa = binding.semi_additive
    adapter = adapter_for(dialect)
    queried_name = query.metrics[0]

    assert binding.source is not None and binding.measure is not None  # noqa: S101
    source_name = binding.source
    alias_to_source = build_alias_tree(source_name, sources_by_name)

    # Stage 2 — dimensions & filters.
    dimensions = _resolve_dimensions(query, sources_by_name, source_name, alias_to_source)
    referenced = {alias for alias, _ in dimensions}
    where_conditions, filter_sources = _bind_filters(
        query.filters, sources_by_name, source_name, alias_to_source
    )
    referenced |= filter_sources
    referenced |= {source_name}

    # Stage 3 — join graph.
    join_edges = plan_joins(
        source_name, referenced - {source_name}, sources_by_name, via=list(query.via) or None
    )

    # Stage 4 — safety floor.
    fanout = any(edge.join.relationship in _FANOUT for edge in join_edges)
    if fanout:
        raise FanoutUnsafe(
            f"semi_additive metric {queried_name!r} cannot be used with a "
            f"one_to_many/many_to_many join; request it without the fanning dimension"
        )

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

    # population_filter — defines the population this metric is compiled over (§4.5); before guardrails.
    where_conditions += _population_filter_conditions(
        binding.binding.canonical.population_filter, sources_by_name, source_name, alias_to_source
    )

    # Stage 6 — guardrails.
    resolved_metric = _ResolvedMetric(name=queried_name, source=source_name, measure=measure_obj)
    guard_conditions, fired = _enforce_guardrails(
        [resolved_metric], resolver, query.context, sources_by_name
    )
    where_conditions += guard_conditions

    # Resolve collapse_dimension to (alias, Dimension).
    collapse_dim_result = _find_dimension(
        sa.collapse_dimension, sources_by_name, source_name, alias_to_source
    )
    if collapse_dim_result is None:
        raise Unresolved(
            f"semi_additive binding {queried_name!r}: collapse_dimension "
            f"{sa.collapse_dimension!r} is not declared on any source"
        )
    collapse_alias, collapse_dim = collapse_dim_result

    # Resolve the source's natural grain (minus collapse_dimension) — this is the
    # partition key for "last/first per entity". It must not be derived from the
    # queried output dimensions: a scalar query (no dimensions) still needs to dedupe
    # per grain entity before summing, otherwise ROW_NUMBER() ranks the whole table
    # and only one arbitrary row survives (SPEC §4.2).
    grain_dims: list[tuple[str, Dimension]] = []
    for grain_col in source_obj.grain:
        if grain_col == sa.collapse_dimension:
            continue
        grain_dim_result = _find_dimension(grain_col, sources_by_name, source_name, alias_to_source)
        if grain_dim_result is None:
            raise Unresolved(
                f"semi_additive binding {queried_name!r}: grain column {grain_col!r} of "
                f"source {source_name!r} is not declared as a dimension"
            )
        grain_dims.append(grain_dim_result)

    # Branch: is collapse_dimension among the grouped dimensions?
    grouped = {dim.name for _alias, dim in dimensions}
    collapsed = sa.collapse_dimension not in grouped

    # Stage 7 — emit SQL.
    ast: exp.Expression
    if not collapsed:
        ast = _build_simple(
            source_name,
            [resolved_metric],
            dimensions,
            where_conditions,
            join_edges,
            sources_by_name,
        )
    else:
        ast = _build_semi_additive(
            owner=source_name,
            measure=measure_obj,
            metric_name=queried_name,
            collapse_alias=collapse_alias,
            collapse_dim=collapse_dim,
            dimensions=dimensions,
            grain_dims=grain_dims,
            where_conditions=where_conditions,
            join_edges=join_edges,
            sources_by_name=sources_by_name,
            collapse_agg=sa.collapse_agg,
        )

    sql = adapter.emit(ast, limit=query.limit)

    # Stage 8 — result metadata.
    used_sources = sorted({source_name} | {e.join.to for e in join_edges})
    return CompileResult(
        sql=sql,
        dialect=adapter.dialect,
        resolved={queried_name: f"{source_name}.{measure_obj.name}"},
        guardrails_fired=fired,
        freshness=[_freshness(sources_by_name[s]) for s in used_sources],
        warnings=[],
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
) -> exp.Expression:
    """Emit the window or nested-aggregate SQL for a semi_additive collapse (SPEC §4.2).

    ``last``/``first`` → ROW_NUMBER() window CTE then filter rn = 1.
    ``avg``/``min``/``max`` → per_snapshot CTE with two-level GROUP BY.
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
        _RANKED = "ranked"
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

        return cast("exp.Expression", outer.with_(_RANKED, as_=inner))

    # avg / min / max — nested GROUP BY form.
    _PER_SNAP = "per_snapshot"
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

    return cast("exp.Expression", outer.with_(_PER_SNAP, as_=inner))
