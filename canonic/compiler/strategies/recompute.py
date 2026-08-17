"""Recompute-at-grain compile path (distinct_count / percentile, SPEC §4.3)."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from sqlglot import exp

from canonic.compiler.compose import LeafRef, MetricLeaves, MetricPlan
from canonic.compiler.dialect import DialectAdapter, adapter_for
from canonic.compiler.joins import JoinEdge, build_alias_tree
from canonic.compiler.leaf import AuxCte, LeafContext, LeafInputs, LeafMetric, plan_leaf
from canonic.compiler.result import RecomputeAtGrainMetadata
from canonic.contracts.models import BindingKind
from canonic.exc import FanoutUnsafe
from canonic.exc import Unreachable as UnreachableError
from canonic.semantic.models import Additivity, Measure

if TYPE_CHECKING:
    from collections.abc import Sequence

    from canonic.compiler.query import SemanticQuery
    from canonic.contracts.principal import EffectivePolicy, Principal
    from canonic.contracts.resolver import Binding as ResolverBinding
    from canonic.contracts.resolver import ContractResolver, RecomputeAtGrainBinding
    from canonic.semantic.models import Dimension, SemanticSource

from canonic.compiler._helpers import (
    _alias,
    _bind_name,
    _dimension_expr,
    _dimension_output_names,
    _from_and_joins,
    _func,
    _parse,
    _qualify_to,
    _ResolvedMetric,
)


def plan_metric(
    query: SemanticQuery,
    queried_name: str,
    binding: ResolverBinding,
    resolver: ContractResolver,
    sources_by_name: dict[str, SemanticSource],
    *,
    dialect: str = "postgres",
    principal: Principal,
    effective_policy: EffectivePolicy,
) -> MetricLeaves:
    """Plan a recompute_at_grain metric (distinct_count / percentile) as one leaf (§4.3).

    Never derives from a pre-aggregate: the leaf always groups the base table by the
    requested dimensions and computes the aggregate directly.

    Fanout policy is kind-specific, which is why this leaf brings its own builder rather
    than the additive floor: a distinct count tolerates row duplication because DISTINCT
    removes it again, while a sort-based quantile is corrupted by it outright.
    """
    assert binding.recompute_at_grain is not None  # noqa: S101 — routing guarantees this kind
    rg = binding.recompute_at_grain
    adapter = adapter_for(dialect)

    assert binding.source is not None  # noqa: S101 — enforced by model_validator
    source_name = binding.source
    alias_to_source = build_alias_tree(source_name, sources_by_name)

    # Resolve the referenced column up front: guardrail matching keys on the physical
    # name, and join planning has to know which alias it lives on.
    col_name = rg.distinct_on if rg.kind is BindingKind.DISTINCT_COUNT else rg.column
    assert col_name is not None  # noqa: S101 — enforced by model_validator
    col_binding = _bind_name(col_name, sources_by_name, source_name, alias_to_source)
    if col_binding is None:
        kind_field = "distinct_on" if rg.kind is BindingKind.DISTINCT_COUNT else "column"
        raise UnreachableError(
            f"metric {queried_name!r}: {kind_field} {col_name!r} is not declared on "
            f"source {source_name!r} or any reachable join"
        )
    col_alias, col_phys = col_binding

    def build(
        inputs: LeafInputs, leaf_metrics: Sequence[LeafMetric]
    ) -> tuple[exp.Expression, tuple[AuxCte, ...]]:
        if inputs.fanout and rg.kind is BindingKind.PERCENTILE:
            raise FanoutUnsafe(
                f"percentile metric {queried_name!r} cannot be used with a "
                f"one_to_many/many_to_many join; row duplication corrupts a sort-based "
                f"quantile — request it without the fanning dimension"
            )
        # distinct_count tolerates fanout: DISTINCT deduplicates the multiplied rows, and
        # the LEFT joins the compiler emits never shrink the population it counts over.
        return _build_recompute(
            owner=inputs.owner,
            rg=rg,
            col_alias=col_alias,
            col_phys=col_phys,
            metric_name=queried_name,
            dimensions=inputs.dimensions,
            where_conditions=inputs.where_conditions,
            join_edges=inputs.join_edges,
            sources_by_name=sources_by_name,
            adapter=adapter,
            name_prefix=inputs.name_prefix,
        )

    # A recompute binding names a column, not a declared measure. The synthetic measure
    # carries the physical column name so source-wide guardrails (applies_to: { source })
    # still match, without ever colliding with a real measure name.
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
                    name=queried_name,
                    source=source_name,
                    measure=_make_synthetic_measure(col_phys),
                ),
                population_filter=binding.binding.canonical.population_filter,
                alias=queried_name,
            )
        ],
        strategy=str(rg.kind),
        strategy_params=(
            ("column", col_name),
            ("quantile", "" if rg.quantile is None else str(rg.quantile)),
        ),
        builder=build,
        fusable=False,
    )

    warnings: tuple[str, ...] = ()
    if rg.kind is BindingKind.PERCENTILE and not adapter.supports_percentile_cont():
        warnings = (
            f"metric {queried_name!r}: dialect {adapter.dialect!r} has no native percentile "
            f"aggregate — computed via nearest-rank window function, which returns an actual "
            f"row value rather than a linearly interpolated one (differs from PERCENTILE_CONT "
            f"on even-sized groups)",
        )

    return MetricLeaves(
        leaves=[leaf],
        metric=MetricPlan(name=queried_name, refs=(LeafRef(leaf=0, column=queried_name),)),
        resolved=f"recompute_at_grain({source_name}.{col_name})",
        recompute_at_grain=RecomputeAtGrainMetadata(
            kind=str(rg.kind),
            distinct_on=rg.distinct_on,
            column=rg.column,
            quantile=rg.quantile,
        ),
        warnings=warnings,
    )


def _build_recompute(
    owner: str,
    rg: RecomputeAtGrainBinding,
    col_alias: str,
    col_phys: str,
    metric_name: str,
    dimensions: list[tuple[str, Dimension]],
    where_conditions: list[exp.Expression],
    join_edges: list[JoinEdge],
    sources_by_name: dict[str, SemanticSource],
    adapter: DialectAdapter,
    name_prefix: str = "",
) -> tuple[exp.Expression, tuple[AuxCte, ...]]:
    """Build the recompute-at-grain SELECT: group base table by dims, aggregate directly.

    distinct_count → COUNT(DISTINCT <col>)
    percentile     → PERCENTILE_CONT(q) WITHIN GROUP (ORDER BY <col>) where the dialect has
                      a native ordered-set aggregate; otherwise a CUME_DIST() window-function
                      fallback (see :func:`_build_percentile_fallback`).

    Never wraps in DISTINCT ON dedup — the grain is always recomputed from base rows.
    """
    if rg.kind is BindingKind.DISTINCT_COUNT:
        qualified_col = exp.column(col_phys, table=col_alias)
        agg_expr: exp.Expression = cast(
            "exp.Expression",
            exp.Count(this=exp.Distinct(expressions=[qualified_col])),
        )
    elif not adapter.supports_percentile_cont():
        assert rg.quantile is not None  # noqa: S101 — enforced by model_validator
        return _build_percentile_fallback(
            owner=owner,
            quantile=rg.quantile,
            col_alias=col_alias,
            col_phys=col_phys,
            metric_name=metric_name,
            dimensions=dimensions,
            where_conditions=where_conditions,
            join_edges=join_edges,
            sources_by_name=sources_by_name,
            name_prefix=name_prefix,
        )
    else:
        # PERCENTILE: parse PERCENTILE_CONT(q) WITHIN GROUP (ORDER BY col), then qualify.
        assert rg.quantile is not None  # noqa: S101 — enforced by model_validator
        agg_expr = _qualify_to(
            _parse(f"PERCENTILE_CONT({rg.quantile}) WITHIN GROUP (ORDER BY {col_phys})"),
            col_alias,
        )

    select = exp.Select()
    projections: list[exp.Expression] = []
    group_exprs: list[exp.Expression] = []
    for (src, dim), name in zip(dimensions, _dimension_output_names(dimensions), strict=True):
        expr = _dimension_expr(src, dim)
        projections.append(_alias(expr, name))
        group_exprs.append(expr)
    projections.append(_alias(agg_expr, metric_name))
    select = select.select(*projections)
    select = _from_and_joins(select, owner, join_edges, sources_by_name)
    if where_conditions:
        select = select.where(exp.and_(*where_conditions))
    if group_exprs:
        select = select.group_by(*group_exprs)
    return select, ()


def _build_percentile_fallback(
    owner: str,
    quantile: float,
    col_alias: str,
    col_phys: str,
    metric_name: str,
    dimensions: list[tuple[str, Dimension]],
    where_conditions: list[exp.Expression],
    join_edges: list[JoinEdge],
    sources_by_name: dict[str, SemanticSource],
    name_prefix: str = "",
) -> tuple[exp.Expression, tuple[AuxCte, ...]]:
    """Build a percentile recompute for dialects without an ordered-set aggregate (e.g. SQLite).

    Ranks each row within its dimension partition via ``CUME_DIST()`` and picks the smallest
    value at or past the target quantile — the standard window-function substitute for
    engines with no ``PERCENTILE_CONT() WITHIN GROUP`` support. This is a nearest-rank
    (percentile_disc-style) result: for even-sized groups it returns an actual row value
    rather than the linear interpolation between the two middle values that PERCENTILE_CONT
    would produce.
    """
    _RANKED = f"{name_prefix}_ranked"
    _VAL = "_val"
    _CD = "_cd"

    col_expr = exp.column(col_phys, table=col_alias)

    dim_names = _dimension_output_names(dimensions)
    inner = exp.Select()
    inner_projections: list[exp.Expression] = []
    partition_exprs: list[exp.Expression] = []
    for (src, dim), name in zip(dimensions, dim_names, strict=True):
        expr = _dimension_expr(src, dim)
        inner_projections.append(_alias(expr, name))
        partition_exprs.append(expr)
    inner_projections.append(_alias(col_expr, _VAL))
    cume_dist = cast(
        "exp.Expression",
        exp.Window(
            this=exp.CumeDist(),
            partition_by=partition_exprs,
            order=exp.Order(expressions=[exp.Ordered(this=col_expr)]),
        ),
    )
    inner_projections.append(_alias(cume_dist, _CD))
    inner = inner.select(*inner_projections)
    inner = _from_and_joins(inner, owner, join_edges, sources_by_name)
    # PERCENTILE_CONT/QUANTILE_CONT (the native aggregates this fallback stands in for) ignore
    # NULLs; CUME_DIST must exclude them too or the quantile threshold lands on the wrong rank.
    not_null = _qualify_to(_parse(f"{col_phys} IS NOT NULL"), col_alias)
    inner_where = [*where_conditions, not_null]
    inner = inner.where(exp.and_(*inner_where))

    outer = exp.Select()
    outer_projections: list[exp.Expression] = []
    outer_group: list[exp.Expression] = []
    for name in dim_names:
        dim_col = cast("exp.Expression", exp.column(name, table=_RANKED))
        outer_projections.append(_alias(dim_col, name))
        outer_group.append(dim_col)
    val_col = cast("exp.Expression", exp.column(_VAL, table=_RANKED))
    outer_projections.append(_alias(_func("MIN", val_col), metric_name))
    outer = outer.select(*outer_projections)
    outer = outer.from_(exp.to_table(_RANKED))
    cd_col = cast("exp.Expression", exp.column(_CD, table=_RANKED))
    outer = outer.where(exp.GTE(this=cd_col, expression=exp.Literal.number(quantile)))
    if outer_group:
        outer = outer.group_by(*outer_group)

    return outer, (AuxCte(name=_RANKED, body=inner),)


def _make_synthetic_measure(col_name: str) -> Measure:
    """Build a minimal non-additive Measure for guardrail enforcement in recompute_at_grain.

    recompute_at_grain bindings reference a column, not a declared measure. Source-wide
    guardrails (applies_to: { source }, measure: None) match on source alone, so the
    synthetic name never reaches any equality check that would cause a false positive.
    """
    return Measure(name=col_name, expr=col_name, additivity=Additivity.NON_ADDITIVE)
