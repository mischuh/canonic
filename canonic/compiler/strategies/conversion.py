"""Conversion compile path: conversion events attributed to the latest earlier base event.

A conversion relates two event streams through an entity. Each conversion event counts for the
latest base event of the same entity at or before it, within the window, and is reported under
that base event's dimension values. The base metric is planned as if it were requested alone
and supplies the denominator of the rate. The conversions are one more leaf, rooted at the base
metric's source, built from CTEs it hoists into the statement's single outer ``WITH``:

* ``base``: the base events with the requested dimensions, the entity, the time and the
  constant properties, scoped like the base metric queried alone, query filters included.
* ``conversions``: the conversion source's rows, scoped by the conversion metric's own
  ``population_filter``, guardrails and tenant predicates only. They are planned as a leaf of
  their own without the query's dimensions and filters, so that scoping is applied by the same
  code path as everywhere else.
* ``attributed``: every match of a conversion event to a base event, ranked so the latest base
  event comes first.

The leaf then computes the conversion metric over the first-ranked matches, grouped by the
dimension values of their base events.
"""

from __future__ import annotations

import dataclasses
from typing import TYPE_CHECKING, cast

from sqlglot import exp

from canonic.compiler._helpers import (
    _active_adapter,
    _alias,
    _dim_mask_strategy,
    _dimension_expr,
    _from_and_joins,
    _measure_expr,
    _ResolvedMetric,
    current_time_shifts,
)
from canonic.compiler.compose import (
    Combine,
    LeafRef,
    MetricLeaves,
    MetricPlan,
    guard_division,
    operand_placeholder,
    zero_denominator_warning,
)
from canonic.compiler.leaf import AuxCte, LeafContext, LeafMetric, _render, plan_leaf
from canonic.compiler.result import ConversionMetadata
from canonic.compiler.strategies.recompute import _make_synthetic_measure
from canonic.compiler.strategies.simple_additive import _bind_metric
from canonic.contracts.models import BindingKind, ConversionCalculation, OnZeroDenominator
from canonic.exc import FanoutUnsafe, Unresolved, UnsupportedMeasure
from canonic.semantic.models import NormalizedType

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from canonic.compiler.leaf import LeafInputs, LeafPlan
    from canonic.compiler.query import SemanticQuery
    from canonic.contracts.principal import EffectivePolicy, Principal
    from canonic.contracts.resolver import Binding as ResolverBinding
    from canonic.contracts.resolver import ContractResolver, ConversionBinding
    from canonic.semantic.models import Column, SemanticSource

__all__ = ["plan_metric"]

_ENTITY = "_entity"
_EVENT_TIME = "_event_time"
_PROPERTY = "_property_{}"
_KEY = "_conversion_key_{}"
_ATTRIBUTION = "_attribution"
_BASE_EVENT = "_base_event"
_ATTRIBUTED = "_attributed"

_ZERO_POLICY: dict[OnZeroDenominator, Combine] = {
    OnZeroDenominator.NULL: Combine.RATIO_NULL,
    OnZeroDenominator.ZERO: Combine.RATIO_ZERO,
    OnZeroDenominator.ERROR: Combine.RATIO_RAW,
}


@dataclasses.dataclass(frozen=True, slots=True)
class _Side:
    """One side of the match: its metric bound to a source, and the columns it is matched on."""

    resolved: _ResolvedMetric
    population_filter: str | None
    entity: Column
    time: Column
    properties: tuple[Column, ...]


def plan_metric(
    query: SemanticQuery,
    queried_name: str,
    binding: ResolverBinding,
    resolver: ContractResolver,
    sources_by_name: dict[str, SemanticSource],
    *,
    principal: Principal,
    effective_policy: EffectivePolicy,
    plan_base: Callable[[str, ResolverBinding], MetricLeaves],
) -> MetricLeaves:
    """Plan the base metric, the conversions leaf, and the expression that combines them."""
    assert binding.conversion is not None  # noqa: S101 — routing guarantees conversion kind
    conversion = binding.conversion
    if current_time_shifts():
        raise UnsupportedMeasure(
            f"conversion metric {queried_name!r} cannot be read under an offset yet"
        )
    base = _side(queried_name, conversion, "base", sources_by_name)
    converted = _side(queried_name, conversion, "conversion", sources_by_name)
    ctx = LeafContext(
        query=query,
        resolver=resolver,
        sources_by_name=sources_by_name,
        principal=principal,
        effective_policy=effective_policy,
    )
    events = _plan_conversion_events(queried_name, ctx, converted)
    measure = _conversion_measure(queried_name, conversion.conversion, converted, sources_by_name)
    leaf = _plan_conversions_leaf(queried_name, ctx, conversion, base, converted, events, measure)

    planned_base = plan_base(conversion.base.metric, conversion.base)
    combine = _ZERO_POLICY[conversion.on_zero_denominator]
    template = _template(conversion.calculation, combine)
    warnings = planned_base.warnings
    if conversion.calculation is ConversionCalculation.CONVERSION_RATE and (
        combine is Combine.RATIO_NULL
    ):
        warnings = (*warnings, zero_denominator_warning(queried_name))

    conversions_operand = MetricPlan(
        name=queried_name, refs=(LeafRef(leaf=0, column=queried_name),)
    )
    base_operand = planned_base.offset(1)
    return MetricLeaves(
        leaves=[leaf, *planned_base.leaves],
        metric=MetricPlan(
            name=queried_name,
            refs=(*conversions_operand.refs, *base_operand.refs),
            combine=Combine.DERIVED,
            operands=(conversions_operand, base_operand),
            template=template,
        ),
        resolved=binding.resolved_key or queried_name,
        offsets=planned_base.offsets,
        derived=planned_base.derived,
        conversions=(_metadata(queried_name, conversion), *planned_base.conversions),
        warnings=warnings,
    )


def _side(
    metric: str,
    conversion: ConversionBinding,
    side: str,
    sources_by_name: dict[str, SemanticSource],
) -> _Side:
    """Bind one side's metric and resolve its entity, time and constant property columns."""
    if side == "base":
        bound, entity, time = conversion.base, conversion.entity[0], conversion.base_time
        properties = [p[0] for p in conversion.constant_properties]
    else:
        bound, entity, time = (
            conversion.conversion,
            conversion.entity[1],
            conversion.conversion_time,
        )
        properties = [p[1] for p in conversion.constant_properties]
    if bound.kind not in (BindingKind.SINGLE, BindingKind.DISTINCT_COUNT):
        raise UnsupportedMeasure(
            f"conversion metric {metric!r}: {side} metric {bound.metric!r} has kind "
            f"{bound.kind.value}; a conversion reads metrics of kind single or distinct_count"
        )
    assert bound.source is not None  # noqa: S101 — set for both source-bound kinds
    source = sources_by_name.get(bound.source)
    if source is None:
        raise Unresolved(f"metric {bound.metric!r} binds to unknown source {bound.source!r}")
    if bound.kind is BindingKind.SINGLE:
        resolved = _bind_metric(bound.metric, bound, sources_by_name)
    else:
        assert bound.recompute_at_grain is not None  # noqa: S101 — set for distinct_count
        distinct_on = bound.recompute_at_grain.distinct_on or ""
        resolved = _ResolvedMetric(
            name=bound.metric, source=bound.source, measure=_make_synthetic_measure(distinct_on)
        )

    def column(field: str, name: str) -> Column:
        found = source.own_column(name)
        if found is None:
            raise Unresolved(
                f"conversion metric {metric!r}: {field} {name!r} is not a column, or a dimension "
                f"backed by one column, of source {source.name!r}"
            )
        return found

    return _Side(
        resolved=resolved,
        population_filter=bound.binding.canonical.population_filter,
        entity=column(f"{side} entity", entity),
        time=column(f"{side}_time", time),
        properties=tuple(column(f"{side} constant property", p) for p in properties),
    )


def _plan_conversion_events(metric: str, ctx: LeafContext, converted: _Side) -> LeafPlan:
    """The conversion source's rows under its own scoping, as a leaf without the query's own.

    The query's dimensions, filters and ``time_range`` describe the base events. Planned with
    them removed, the leaf still applies the conversion metric's ``population_filter``, its
    guardrails and the principal's tenant predicates.
    """
    owner = converted.resolved.source

    def build(
        inputs: LeafInputs, _metrics: Sequence[LeafMetric]
    ) -> tuple[exp.Expression, tuple[AuxCte, ...]]:
        if inputs.fanout:
            raise FanoutUnsafe(
                f"conversion metric {metric!r}: the scoping of source {owner!r} needs a "
                f"one_to_many/many_to_many join, which would repeat its conversion events"
            )
        select = exp.Select().select(exp.Column(this=exp.Star(), table=exp.to_identifier(owner)))
        select = _from_and_joins(select, owner, inputs.join_edges, inputs.ctx.sources_by_name)
        if inputs.where_conditions:
            select = select.where(exp.and_(*inputs.where_conditions))
        return select, ()

    return plan_leaf(
        dataclasses.replace(
            ctx,
            query=ctx.query.model_copy(
                update={"dimensions": [], "filters": [], "time_range": None}
            ),
        ),
        owner,
        [LeafMetric(resolved=converted.resolved, population_filter=converted.population_filter)],
        strategy="conversion_events",
        builder=build,
        fusable=False,
    )


def _conversion_measure(
    metric: str,
    bound: ResolverBinding,
    converted: _Side,
    sources_by_name: dict[str, SemanticSource],
) -> exp.Expression:
    """The conversion metric's aggregate over the conversion source's own columns."""
    owner = converted.resolved.source
    if bound.kind is BindingKind.SINGLE:
        return _measure_expr(owner, converted.resolved.measure)
    column = sources_by_name[owner].own_column(converted.resolved.measure.name)
    if column is None:
        raise Unresolved(
            f"conversion metric {metric!r}: conversion metric {bound.metric!r} counts "
            f"{converted.resolved.measure.name!r}, which is not a column of source {owner!r}"
        )
    return cast(
        "exp.Expression",
        exp.Count(this=exp.Distinct(expressions=[exp.column(column.name, table=owner)])),
    )


def _plan_conversions_leaf(
    metric: str,
    ctx: LeafContext,
    conversion: ConversionBinding,
    base: _Side,
    converted: _Side,
    events: LeafPlan,
    measure: exp.Expression,
) -> LeafPlan:
    owner = base.resolved.source

    def build(
        inputs: LeafInputs, leaf_metrics: Sequence[LeafMetric]
    ) -> tuple[exp.Expression, tuple[AuxCte, ...]]:
        if inputs.fanout:
            raise FanoutUnsafe(
                f"conversion metric {metric!r} cannot be grouped or filtered through a "
                f"one_to_many/many_to_many join: a base event repeated under several dimension "
                f"values would count its conversions under one of them at random"
            )
        return _build_conversions(
            inputs,
            leaf_metrics[0].alias or metric,
            conversion=conversion,
            base=base,
            converted=converted,
            events=events.select,
            measure=measure,
        )

    leaf = plan_leaf(
        ctx,
        owner,
        [
            LeafMetric(
                resolved=base.resolved, population_filter=base.population_filter, alias=metric
            )
        ],
        strategy="conversion",
        strategy_params=(
            ("events", _render(events.select)),
            ("measure", _render(measure)),
            ("entity", f"{base.entity.name}={converted.entity.name}"),
            ("time", f"{base.time.name}<={converted.time.name}"),
            ("window", str(conversion.window) if conversion.window is not None else ""),
            (
                "properties",
                "\x00".join(
                    f"{b.name}={c.name}"
                    for b, c in zip(base.properties, converted.properties, strict=True)
                ),
            ),
        ),
        builder=build,
        fusable=False,
    )
    seen = {g.id for g in leaf.fired}
    return dataclasses.replace(
        leaf,
        fired=(*leaf.fired, *(g for g in events.fired if g.id not in seen)),
        warnings=(*leaf.warnings, *(w for w in events.warnings if w not in leaf.warnings)),
        used_sources=leaf.used_sources | events.used_sources,
        scoped_sources=leaf.scoped_sources | events.scoped_sources,
        shared_sources=leaf.shared_sources | events.shared_sources,
    )


def _build_conversions(
    inputs: LeafInputs,
    alias: str,
    *,
    conversion: ConversionBinding,
    base: _Side,
    converted: _Side,
    events: exp.Expression,
    measure: exp.Expression,
) -> tuple[exp.Expression, tuple[AuxCte, ...]]:
    prefix = inputs.name_prefix or "_"
    base_cte, events_cte, attributed_cte = (
        f"{prefix}{suffix}" for suffix in ("base", "conversions", "attributed")
    )
    owner = inputs.owner
    target = converted.resolved.source
    dim_names = inputs.dim_names

    projections: list[exp.Expression] = []
    group: list[exp.Expression] = []
    for (src, dim), name in zip(inputs.dimensions, dim_names, strict=True):
        expr = _dimension_expr(src, dim, _dim_mask_strategy(inputs.dim_mask, src, dim))
        projections.append(_alias(expr, name))
        group.append(expr)
    matched_on = [
        (_ENTITY, base.entity),
        (_EVENT_TIME, base.time),
        *((_PROPERTY.format(i), column) for i, column in enumerate(base.properties)),
    ]
    for name, column in matched_on:
        expr = cast("exp.Expression", exp.column(column.name, table=owner))
        projections.append(_alias(expr, name))
        group.append(expr.copy())
    base_select = _from_and_joins(
        exp.Select().select(*projections),
        owner,
        inputs.join_edges,
        inputs.ctx.sources_by_name,
    )
    if inputs.where_conditions:
        base_select = base_select.where(exp.and_(*inputs.where_conditions))
    base_select = base_select.group_by(*group)

    def base_column(name: str) -> exp.Expression:
        return cast("exp.Expression", exp.column(name, table=_BASE_EVENT))

    def target_column(column: Column) -> exp.Expression:
        return cast("exp.Expression", exp.column(column.name, table=target))

    adapter = _active_adapter()
    conditions: list[exp.Expression] = [
        exp.EQ(this=base_column(_ENTITY), expression=target_column(converted.entity)),
        exp.LTE(this=base_column(_EVENT_TIME), expression=target_column(converted.time)),
    ]
    window = conversion.window
    if window is not None:
        conditions.append(
            exp.LTE(
                this=target_column(converted.time),
                expression=adapter.date_add(
                    base_column(_EVENT_TIME),
                    window.count,
                    window.unit,
                    as_date=base.time.type is NormalizedType.DATE,
                ),
            )
        )
    conditions += [
        exp.EQ(this=base_column(_PROPERTY.format(i)), expression=target_column(column))
        for i, column in enumerate(converted.properties)
    ]

    grain = inputs.ctx.sources_by_name[target].grain
    keys = [cast("exp.Expression", exp.column(g, table=target)) for g in grain]
    rank = exp.Window(
        this=exp.RowNumber(),
        partition_by=[k.copy() for k in keys],
        order=exp.Order(
            expressions=[
                exp.Ordered(this=base_column(_EVENT_TIME), desc=True),
                *(exp.Ordered(this=base_column(name)) for name in dim_names),
            ]
        ),
    )
    attributed_select = (
        exp.Select()
        .select(
            *(_alias(k, _KEY.format(i)) for i, k in enumerate(keys)),
            *(_alias(base_column(name), name) for name in dim_names),
            _alias(cast("exp.Expression", rank), _ATTRIBUTION),
        )
        .from_(_alias(exp.to_table(events_cte), target))
        .join(
            _alias(exp.to_table(base_cte), _BASE_EVENT), on=exp.and_(*conditions), join_type="INNER"
        )
    )

    def attributed_column(name: str) -> exp.Expression:
        return cast("exp.Expression", exp.column(name, table=_ATTRIBUTED))

    joined_on = [
        exp.EQ(this=attributed_column(_KEY.format(i)), expression=k.copy())
        for i, k in enumerate(keys)
    ]
    select = (
        exp.Select()
        .select(
            *(_alias(attributed_column(name), name) for name in dim_names),
            _alias(measure.copy(), alias),
        )
        .from_(_alias(exp.to_table(events_cte), target))
        .join(_alias(exp.to_table(attributed_cte), _ATTRIBUTED), on=exp.and_(*joined_on))
        .where(exp.EQ(this=attributed_column(_ATTRIBUTION), expression=exp.Literal.number(1)))
    )
    if dim_names:
        select = select.group_by(*(attributed_column(name) for name in dim_names))
    return select, (
        AuxCte(name=base_cte, body=base_select),
        AuxCte(name=events_cte, body=events),
        AuxCte(name=attributed_cte, body=attributed_select),
    )


def _template(calculation: ConversionCalculation, combine: Combine) -> exp.Expression:
    """The value from the conversions (``_op0``) and the base metric (``_op1``).

    No attributed conversion is ``0`` wherever the base metric has a row, and ``NULL`` where it
    has none, since there was nothing to convert.
    """
    conversions = cast("exp.Expression", exp.column(operand_placeholder(0)))
    base = cast("exp.Expression", exp.column(operand_placeholder(1)))
    if calculation is ConversionCalculation.CONVERSION_RATE:
        counted = cast("exp.Expression", exp.func("COALESCE", conversions, exp.Literal.number(0)))
        return guard_division(counted, base, combine)
    zero_with_base = exp.Case().when(
        exp.Not(this=exp.Is(this=base, expression=exp.null())), exp.Literal.number(0)
    )
    return cast("exp.Expression", exp.func("COALESCE", conversions, zero_with_base))


def _metadata(metric: str, conversion: ConversionBinding) -> ConversionMetadata:
    return ConversionMetadata(
        metric=metric,
        base_metric=conversion.base.metric,
        conversion_metric=conversion.conversion.metric,
        entity={"base": conversion.entity[0], "conversion": conversion.entity[1]},
        base_time=conversion.base_time,
        conversion_time=conversion.conversion_time,
        window=str(conversion.window) if conversion.window is not None else None,
        calculation=conversion.calculation.value,
        constant_properties=[
            {"base": b, "conversion": c} for b, c in conversion.constant_properties
        ],
    )
