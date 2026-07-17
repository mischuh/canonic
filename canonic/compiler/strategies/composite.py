"""Composite compile path (composable_post_agg: ratio / weighted_avg, SPEC §4.1)."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from sqlglot import exp

from canonic.compiler.dialect import adapter_for
from canonic.compiler.joins import JoinEdge, build_alias_tree, plan_joins
from canonic.compiler.result import (
    CompileResult,
    CompositionMetadata,
    FinalityMetadata,
    FiredGuardrail,
)
from canonic.contracts.models import BindingKind, OnZeroDenominator
from canonic.exc import FanoutUnsafe, Unresolved, UnsupportedMeasure
from canonic.semantic.models import Additivity, Measure

if TYPE_CHECKING:
    from canonic.compiler.query import SemanticQuery
    from canonic.contracts.resolver import Binding as ResolverBinding
    from canonic.contracts.resolver import ComponentBindings, ContractResolver
    from canonic.semantic.models import Dimension, SemanticSource

from canonic.compiler._helpers import (
    _FANOUT,
    _alias,
    _bind_filters,
    _build_finality_union,
    _dimension_expr,
    _dimension_output_names,
    _enforce_guardrails,
    _find_measure,
    _find_time_dim_name,
    _freshness,
    _from_and_joins,
    _func,
    _measure_expr,
    _population_filter_conditions,
    _resolve_dimensions,
    _ResolvedMetric,
)


class _LeafPlan:
    """A compiled leaf query for one component of a composable_post_agg metric (§4.1, §6)."""

    __slots__ = ("dim_names", "finality", "fired", "select", "used_sources", "warnings")

    def __init__(
        self,
        select: exp.Expression,
        fired: list[FiredGuardrail],
        used_sources: set[str],
        warnings: list[str],
        dim_names: list[str],
        finality: FinalityMetadata | None = None,
    ) -> None:
        self.select = select
        self.fired = fired
        self.used_sources = used_sources
        self.warnings = warnings
        self.dim_names = dim_names
        self.finality = finality


def _plan_leaf(
    component: ResolverBinding,
    query: SemanticQuery,
    resolver: ContractResolver,
    sources_by_name: dict[str, SemanticSource],
    measure_alias: str,
    population_filter: str | None = None,
) -> _LeafPlan:
    """Run stages 2–6 for one single-kind component; return a leaf SELECT.

    Each component is planned as an independent sub-query at the requested grain so
    its own guardrails and safety-floor checks fire automatically (SPEC §4.1, §6, AC3).
    When the component metric has a finality rule and the query includes a time dimension,
    the leaf is emitted as a finality UNION ALL (projecting ``is_final``) — so the compose
    step can inherit the most conservative signal across leaves (§7, S6 AC1).
    """
    if component.kind is not BindingKind.SINGLE:
        raise UnsupportedMeasure(
            f"nested composite metrics are not yet supported; "
            f"component {component.metric!r} has kind {component.kind!r}"
        )
    assert component.source is not None and component.measure is not None  # noqa: S101

    source_name = component.source
    alias_to_source = build_alias_tree(source_name, sources_by_name)

    # Stage 2 — dimensions & filters relative to this component's source.
    dimensions = _resolve_dimensions(query, sources_by_name, source_name, alias_to_source)
    referenced = {alias for alias, _ in dimensions}
    where_conditions, filter_sources = _bind_filters(
        query.filters, sources_by_name, source_name, alias_to_source
    )
    referenced |= filter_sources
    referenced |= {source_name}

    # Stage 3 — join graph from this component's owner.
    join_edges = plan_joins(
        source_name, referenced - {source_name}, sources_by_name, via=list(query.via) or None
    )

    # Stage 4 — safety floor for this component.
    fanout = any(edge.join.relationship in _FANOUT for edge in join_edges)
    grouped = {dim.name for _alias, dim in dimensions}

    source_obj = sources_by_name.get(source_name)
    if source_obj is None:
        raise Unresolved(f"component {component.metric!r} binds to unknown source {source_name!r}")
    measure_obj = _find_measure(source_obj, component.measure)
    if measure_obj is None:
        raise Unresolved(
            f"component {component.metric!r} binds to unknown measure "
            f"{source_name}.{component.measure!r}"
        )

    add = measure_obj.additivity
    if add is Additivity.ADDITIVE:
        if not measure_obj.is_p0_compilable:
            raise UnsupportedMeasure(
                f"measure {source_name}.{measure_obj.name!r} uses an aggregate function "
                f"not supported at P0"
            )
    elif fanout:
        raise FanoutUnsafe(
            f"measure {source_name}.{measure_obj.name!r} is {add.value} and a "
            f"one_to_many/many_to_many join in this query would multiply its rows "
            f"and corrupt the aggregate; request it without the fanning dimension "
            f"or source, or query it at its native grain"
        )
    elif add is Additivity.SEMI_ADDITIVE:
        unsafe_dims = [d for d in measure_obj.semi_additive_over if d not in grouped]
        if unsafe_dims:
            raise UnsupportedMeasure(
                f"measure {source_name}.{measure_obj.name!r} is semi-additive over "
                f"{unsafe_dims} and cannot be collapsed across those dimensions "
                f"without the semi_additive strategy; group by {unsafe_dims} for "
                f"a correct result"
            )

    # population_filter — defines the population this leaf is compiled over (§4.5); before guardrails.
    where_conditions += _population_filter_conditions(
        population_filter, sources_by_name, source_name, alias_to_source
    )

    # Stage 6 — guardrails for this leaf.
    guard_conditions, fired = _enforce_guardrails(
        [_ResolvedMetric(name=component.metric, source=source_name, measure=measure_obj)],
        resolver,
        query.context,
        sources_by_name,
    )
    where_conditions += guard_conditions

    # Stage 5 — finality per leaf (§7, S6): build a UNION ALL when the component metric
    # has a finality rule and the query includes a time dimension, so the compose step
    # can apply the conservative-merge rule across leaves.
    leaf_finality: FinalityMetadata | None = None
    finality_rule = resolver.finality_for(component.metric)
    time_dim_name: str | None = None
    if finality_rule is not None:
        time_dim_name = _find_time_dim_name(dimensions, sources_by_name, alias_to_source)
        if time_dim_name is None:
            finality_rule = None  # no time dimension → all rows implicitly final

    if finality_rule is not None and time_dim_name is not None:
        from canonic.contracts.finality import evaluate_watermark, watermark_to_iso

        final_r = next(r for r in finality_rule.realizations if r.role == "final")
        watermark_dt = evaluate_watermark(
            cast("str", final_r.watermark), cast("str", final_r.tz), query.as_of
        )
        leaf_metrics = [
            _ResolvedMetric(name=component.metric, source=source_name, measure=measure_obj)
        ]
        leaf_select: exp.Expression = _build_finality_union(
            rule=finality_rule,
            query_metrics=leaf_metrics,
            dimensions=dimensions,
            where_conditions=where_conditions,
            sources_by_name=sources_by_name,
            watermark_dt=watermark_dt,
            time_dim_name=time_dim_name,
            original_owner=source_name,
            measure_alias=measure_alias,
        )
        leaf_finality = FinalityMetadata(
            watermark=watermark_to_iso(watermark_dt),
            sources_used=[r.source for r in finality_rule.realizations],
            result_flag=finality_rule.result_flag or "per_row",
        )
        used = {r.source for r in finality_rule.realizations}
    else:
        leaf_select = _build_leaf_select(
            source_name,
            measure_obj,
            measure_alias,
            dimensions,
            where_conditions,
            join_edges,
            sources_by_name,
        )
        used = {source_name} | {e.join.to for e in join_edges}

    return _LeafPlan(
        select=leaf_select,
        fired=fired,
        used_sources=used,
        warnings=[],
        dim_names=_dimension_output_names(dimensions),
        finality=leaf_finality,
    )


def _build_leaf_select(
    owner: str,
    measure: Measure,
    measure_alias: str,
    dimensions: list[tuple[str, Dimension]],
    where_conditions: list[exp.Expression],
    join_edges: list[JoinEdge],
    sources_by_name: dict[str, SemanticSource],
) -> exp.Select:
    """Build a leaf SELECT projecting dimensions + one measure aliased to measure_alias."""
    select = exp.Select()
    projections: list[exp.Expression] = []
    group_exprs: list[exp.Expression] = []
    for (src, dim), name in zip(dimensions, _dimension_output_names(dimensions), strict=True):
        expr = _dimension_expr(src, dim)
        projections.append(_alias(expr, name))
        group_exprs.append(expr)
    projections.append(_alias(_measure_expr(owner, measure), measure_alias))
    select = select.select(*projections)
    select = _from_and_joins(select, owner, join_edges, sources_by_name)
    if where_conditions:
        select = select.where(exp.and_(*where_conditions))
    if group_exprs:
        select = select.group_by(*group_exprs)
    return select


def _compile_composite(
    query: SemanticQuery,
    composite: ResolverBinding,
    resolver: ContractResolver,
    sources_by_name: dict[str, SemanticSource],
    *,
    dialect: str = "postgres",
) -> CompileResult:
    """Compile a composable_post_agg metric via CTE-per-leaf + divide (SPEC §4.1, §6 step 6b).

    The unifying rule — aggregate first, combine last: each component is planned as an
    independent sub-query (stages 2–4, 6) at the requested grain; the compose step
    divides on the shared grain via ``n / NULLIF(d, 0)`` (or variant per on_zero_denominator).
    """
    assert composite.components is not None  # noqa: S101 — routing guarantees composite kind
    components: ComponentBindings = composite.components
    on_zero = components.on_zero_denominator
    adapter = adapter_for(dialect)
    queried_name = query.metrics[0]

    pop_filter = composite.binding.canonical.population_filter
    num_plan = _plan_leaf(components.numerator, query, resolver, sources_by_name, "n", pop_filter)
    den_plan = _plan_leaf(components.denominator, query, resolver, sources_by_name, "d", pop_filter)

    dim_names = num_plan.dim_names

    # Build the division expression per on_zero_denominator policy.
    n_col = cast("exp.Expression", exp.column("n"))
    d_col = cast("exp.Expression", exp.column("d"))
    if on_zero is OnZeroDenominator.NULL:
        nullif_d = exp.func("NULLIF", d_col, exp.Literal.number(0))
        combine = cast("exp.Expression", exp.Div(this=n_col, expression=nullif_d))
        zero_warnings = [
            f"zero denominator for metric {composite.metric!r} yields NULL "
            f"(on_zero_denominator=null)"
        ]
    elif on_zero is OnZeroDenominator.ZERO:
        nullif_d = exp.func("NULLIF", d_col, exp.Literal.number(0))
        raw_div = cast("exp.Expression", exp.Div(this=n_col, expression=nullif_d))
        combine = cast("exp.Expression", exp.func("COALESCE", raw_div, exp.Literal.number(0)))
        zero_warnings = []
    else:  # ERROR — raw division; the engine raises on zero
        combine = cast("exp.Expression", exp.Div(this=n_col, expression=d_col))
        zero_warnings = []

    # Conservative finality merge across leaves (§7, S6 AC1).
    # A composite row is final iff both leaf rows are final; a leaf without a finality rule
    # contributes TRUE (all its rows are implicitly final).
    num_cte: exp.Expression = num_plan.select
    den_cte: exp.Expression = den_plan.select
    composite_finality: FinalityMetadata | None = None

    either_has_finality = num_plan.finality is not None or den_plan.finality is not None
    if either_has_finality:
        # Ensure both CTE selects project is_final (add TRUE for any leaf without a rule).
        if num_plan.finality is None:
            num_cte = cast("exp.Select", num_cte).select(
                _alias(cast("exp.Expression", exp.true()), "is_final")
            )
        if den_plan.finality is None:
            den_cte = cast("exp.Select", den_cte).select(
                _alias(cast("exp.Expression", exp.true()), "is_final")
            )

        # Build conservative FinalityMetadata: earliest watermark + union of sources.
        leaf_finalities = [f for f in [num_plan.finality, den_plan.finality] if f is not None]
        earliest_watermark = min(f.watermark for f in leaf_finalities)
        finality_sources = sorted({s for f in leaf_finalities for s in f.sources_used})
        composite_finality = FinalityMetadata(
            watermark=earliest_watermark,
            sources_used=finality_sources,
            result_flag="per_row",
        )

    # Outer SELECT: dimensions (unqualified — USING merges them) + combined metric.
    outer = exp.Select()
    projections: list[exp.Expression] = []
    for dim_name in dim_names:
        projections.append(_alias(cast("exp.Expression", exp.column(dim_name)), dim_name))
    projections.append(_alias(combine, composite.metric))
    if either_has_finality:
        # is_final = COALESCE(num.is_final, TRUE) AND COALESCE(den.is_final, TRUE)
        num_is_final = cast("exp.Expression", exp.column("is_final", table="num"))
        den_is_final = cast("exp.Expression", exp.column("is_final", table="den"))
        combined_is_final = cast(
            "exp.Expression",
            exp.And(
                this=_func("COALESCE", num_is_final, cast("exp.Expression", exp.true())),
                expression=_func("COALESCE", den_is_final, cast("exp.Expression", exp.true())),
            ),
        )
        projections.append(_alias(combined_is_final, "is_final"))
    outer = outer.select(*projections)

    # Join the two CTEs: FULL JOIN USING (dims) or CROSS JOIN for the scalar case.
    if dim_names:
        outer = outer.from_(exp.to_table("num"))
        # sqlglot stubs use Expr (invariant list) — list[str] is valid at runtime
        outer = outer.join(exp.to_table("den"), using=dim_names, join_type="FULL")  # type: ignore[arg-type]
    else:
        outer = outer.from_(exp.to_table("num"))
        outer = outer.join(exp.to_table("den"), join_type="CROSS")

    # Wrap with CTEs.
    ast = outer.with_("num", as_=num_cte).with_("den", as_=den_cte)

    sql = adapter.emit(ast, limit=query.limit)

    # Stage 8 — assemble result metadata (union guardrails + freshness across leaves).
    fired_ids: set[str] = set()
    fired: list[FiredGuardrail] = []
    for g in num_plan.fired + den_plan.fired:
        if g.id not in fired_ids:
            fired_ids.add(g.id)
            fired.append(g)

    all_sources = num_plan.used_sources | den_plan.used_sources
    freshness = [_freshness(sources_by_name[s]) for s in sorted(all_sources)]

    num_name = components.numerator.metric
    den_name = components.denominator.metric
    resolved_str = (
        f"weighted_avg({num_name}, {den_name})"
        if composite.kind is BindingKind.WEIGHTED_AVG
        else f"ratio({num_name}, {den_name})"
    )

    return CompileResult(
        sql=sql,
        dialect=adapter.dialect,
        resolved={queried_name: resolved_str},
        guardrails_fired=fired,
        freshness=freshness,
        warnings=zero_warnings,
        finality=composite_finality,
        composition=CompositionMetadata(
            kind=composite.kind,
            numerator=num_name,
            denominator=den_name,
            on_zero_denominator=on_zero,
        ),
    )
