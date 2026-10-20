"""Cumulative metrics evaluated per calendar day: trailing windows and period-to-date totals.

A ``ROWS`` frame over the aggregated axis counts rows, not days, and a ``RANGE`` frame with an
interval is not available on SQLite and Redshift. So the frame is a range join instead. The
base measure is aggregated per day and partition, a generated day spine supplies every day the
result can show, and each shown day sums the daily values inside its frame. Everything happens
inside one leaf, from CTEs the leaf hoists into the statement's single outer ``WITH``:

* ``daily``: the base measure per day and partition, built by the additive builders, so
  joins, fanout, tenancy, ``population_filter`` and guardrails apply unchanged.
* ``digits``, ``bounds``, ``spine``: every day from the first day with data to the last one,
  capped at the anchor day (:mod:`canonic.compiler.spine`).
* ``targets``: the spine day that represents each output bucket, the first or last day of a
  bucket under ``period_agg`` ``first`` or ``last``, every day under ``average``.
* ``windows``, only for ``average``: the value per bucket, day and partition.

A filter that reads only the order column decides which days are shown. It is evaluated on the
spine day and never narrows the rows a window reads, so the first shown day carries its full
window. The query's ``time_range`` narrows the daily rows to the range plus the lookback.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from sqlglot import exp

from canonic.compiler._helpers import (
    _active_adapter,
    _alias,
    _bind_filters,
    _build_deduped,
    _build_simple,
    _dim_mask_strategy,
    _dimension_expr,
    _dimension_output_names,
    _find_dimension,
    _func,
    _parse,
    _resolve_dimensions,
)
from canonic.compiler.compose import LeafRef, MetricLeaves, MetricPlan
from canonic.compiler.joins import build_alias_tree
from canonic.compiler.leaf import AuxCte, LeafContext, LeafMetric, _enforce_safety_floor, plan_leaf
from canonic.compiler.result import CumulativeMetadata
from canonic.compiler.spine import DAY_COLUMN, bounds_select, digits_select, spine_select
from canonic.compiler.strategies.cumulative import _split_filters
from canonic.compiler.strategies.simple_additive import _bind_metric
from canonic.compiler.time_range import anchor_day
from canonic.contracts.models import PeriodAgg
from canonic.exc import Unresolved, UnsupportedMeasure
from canonic.semantic.duration import DurationUnit, add_to_date, floor_date
from canonic.semantic.models import PERIOD_GRAINS, Additivity, Dimension, NormalizedType

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from canonic.compiler.leaf import LeafInputs
    from canonic.compiler.query import SemanticQuery
    from canonic.contracts.principal import EffectivePolicy, Principal
    from canonic.contracts.resolver import Binding as ResolverBinding
    from canonic.contracts.resolver import ContractResolver, CumulativeBinding
    from canonic.semantic.models import SemanticSource

__all__ = ["plan_metric"]

_VALUE = "_value"
_BUCKET = "_bucket"
_TARGET = "t"
_DAILY = "d"
_WINDOW = "w"


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
    """Plan a cumulative metric whose value is computed per calendar day."""
    assert binding.cumulative is not None  # noqa: S101 — routing guarantees cumulative kind
    frame = binding.cumulative
    resolved = _bind_metric(queried_name, binding, sources_by_name)
    owner, measure = resolved.source, resolved.measure
    if measure.additivity is not Additivity.ADDITIVE or not measure.is_p0_compilable:
        raise UnsupportedMeasure(
            f"cumulative binding {queried_name!r}: base measure {owner}.{measure.name!r} "
            f"must be additive and use sum, count, min or max"
        )

    alias_to_source = build_alias_tree(owner, sources_by_name)
    dimensions = _resolve_dimensions(query, sources_by_name, owner, alias_to_source)
    order_column = _order_column(
        queried_name, frame.order_by[0], sources_by_name, owner, alias_to_source
    )
    order_index = _requested_on_column(queried_name, order_column, dimensions, frame.order_by[0])
    order_alias, order_dim = dimensions[order_index]
    order_columns = {order_column}
    owner_of_column = sources_by_name[alias_to_source.get(order_alias, order_alias)]
    column_type = next(
        (c.type for c in owner_of_column.columns if c.name == order_column[1]),
        NormalizedType.DATE,
    )
    grain = order_dim.period_grain or (
        "day" if order_dim.value_type(owner_of_column.columns) is NormalizedType.DATE else None
    )
    if grain is None or PERIOD_GRAINS.index(grain) < PERIOD_GRAINS.index("day"):
        raise UnsupportedMeasure(
            f"cumulative metric {queried_name!r} is evaluated per day; group by a dimension "
            f"on {order_column[1]!r} of a day or coarser instead of {order_dim.name!r}"
        )

    regular, visibility = _split_filters(
        queried_name,
        query.filters,
        order_columns,
        sources_by_name,
        owner,
        alias_to_source,
        effective_policy,
    )
    regular += _lookback_filters(
        query, frame, sources_by_name, owner, alias_to_source, order_column
    )
    visibility_conditions, _ = _bind_filters(
        visibility, sources_by_name, owner, alias_to_source, effective_policy
    )
    anchor = (
        exp.Cast(
            this=exp.Literal.string(anchor_day(query.as_of).isoformat()),
            to=exp.DataType.build("DATE"),
        )
        if query.as_of is not None
        else exp.CurrentDate()
    )
    reaggregate = _reaggregate(measure.expr)

    def build(
        inputs: LeafInputs, leaf_metrics: Sequence[LeafMetric]
    ) -> tuple[exp.Expression, tuple[AuxCte, ...]]:
        return _build_windowed(
            inputs,
            leaf_metrics[0],
            order_index=order_index,
            order_column=order_column,
            column_type=column_type,
            frame=frame,
            visibility=visibility_conditions,
            anchor=anchor,
            reaggregate=reaggregate,
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
        strategy="rolling",
        strategy_params=(
            ("frame", frame.frame_key),
            ("visibility", "\x00".join(sorted(visibility))),
            ("anchor", anchor.sql(dialect="postgres")),
        ),
        builder=build,
        fusable=False,
    )

    return MetricLeaves(
        leaves=[leaf],
        metric=MetricPlan(
            name=queried_name, refs=(LeafRef(leaf=0, column=queried_name),), cumulative=True
        ),
        resolved=binding.resolved_key or f"cumulative({owner}.{measure.name})",
        cumulative=CumulativeMetadata(
            metric=queried_name,
            order_by=list(frame.order_by),
            partition_by=[name for i, name in enumerate(query.dimensions) if i != order_index],
            on_gap=str(frame.on_gap),
            history=str(frame.history),
            visibility_filters=list(visibility),
            window=str(frame.window) if frame.window is not None else None,
            grain_to_date=frame.grain_to_date.value if frame.grain_to_date is not None else None,
            period_agg=frame.period_agg.value,
            lookback=_lookback(frame),
        ),
    )


def _order_column(
    metric: str,
    order_by: str,
    sources_by_name: dict[str, SemanticSource],
    owner: str,
    alias_to_source: dict[str, str],
) -> tuple[str, str]:
    """The ``(alias, column)`` the binding's order dimension reads, its day axis."""
    found = _find_dimension(order_by, sources_by_name, owner, alias_to_source)
    if found is None:
        raise Unresolved(
            f"cumulative binding {metric!r}: order_by dimension {order_by!r} is not "
            f"reachable from source {owner!r}"
        )
    alias, dim = found
    columns = dim.backing_columns()
    if len(columns) != 1:
        raise UnsupportedMeasure(
            f"cumulative binding {metric!r}: order_by dimension {order_by!r} must be backed "
            f"by exactly one column"
        )
    return alias, next(iter(columns))


def _requested_on_column(
    metric: str,
    order_column: tuple[str, str],
    dimensions: list[tuple[str, Dimension]],
    order_by: str,
) -> int:
    """The one requested dimension on the order column, which sets the output grain.

    The window is evaluated per day whatever the grain, so ``order_month`` serves a binding
    ordered by ``order_date`` as well as ``order_date`` itself. Two such dimensions in one
    query would ask for two grains of one axis and are refused.
    """
    matches = [
        i
        for i, (alias, dim) in enumerate(dimensions)
        if {(alias, column) for column in dim.backing_columns()} == {order_column}
    ]
    if not matches:
        raise UnsupportedMeasure(
            f"cumulative metric {metric!r} is evaluated along {order_by!r}; add it, or another "
            f"dimension on column {order_column[1]!r}, to the query's dimensions"
        )
    if len(matches) > 1:
        names = ", ".join(repr(dimensions[i][1].name) for i in matches)
        raise UnsupportedMeasure(
            f"cumulative metric {metric!r}: {names} all read column {order_column[1]!r}; "
            f"request one of them"
        )
    return matches[0]


def _lookback(frame: CumulativeBinding) -> str | None:
    if frame.window is not None:
        return str(frame.window)
    if frame.grain_to_date is not None:
        return f"1 {frame.grain_to_date.value}"
    return None


def _lookback_filters(
    query: SemanticQuery,
    frame: CumulativeBinding,
    sources_by_name: dict[str, SemanticSource],
    owner: str,
    alias_to_source: dict[str, str],
    order_column: tuple[str, str],
) -> list[str]:
    """Narrow the daily rows to a ``time_range`` on the order column, widened by the lookback.

    The range itself only decides which days are shown. Rows before its start still count in
    the windows of its first days, so the lower bound moves back by the window, or to the start
    of the ``grain_to_date`` period. An unbounded frame reads every row before the range.
    """
    time_range = query.time_range
    if time_range is None:
        return []
    found = _find_dimension(time_range.dimension, sources_by_name, owner, alias_to_source)
    if found is None:
        return []
    alias, dim = found
    if {(alias, column) for column in dim.backing_columns()} != {order_column}:
        return []
    filters: list[str] = []
    if time_range.start is not None:
        if frame.window is not None:
            filters.append(
                f"{time_range.dimension} >= "
                f"'{add_to_date(time_range.start, frame.window, sign=-1).isoformat()}'"
            )
        elif frame.grain_to_date is not None:
            filters.append(
                f"{time_range.dimension} >= "
                f"'{floor_date(time_range.start, frame.grain_to_date).isoformat()}'"
            )
    if time_range.end is not None:
        filters.append(f"{time_range.dimension} < '{time_range.end.isoformat()}'")
    return filters


def _reach(frame: CumulativeBinding) -> Callable[[exp.Expression], exp.Expression] | None:
    """The last day whose frame can still contain a given day, ``None`` for the day itself.

    A day after the last row of data still has a value while its window reaches back to that
    row, so the spine runs on until the window, or the ``grain_to_date`` period, has passed.
    An unbounded frame never empties and stops at the last row instead.
    """
    adapter = _active_adapter()
    if frame.window is not None:
        window = frame.window

        def through_window(day: exp.Expression) -> exp.Expression:
            ahead = adapter.date_add(day, window.count, window.unit)
            return adapter.date_add(ahead, -1, DurationUnit.DAY)

        return through_window
    if frame.grain_to_date is not None:
        grain = frame.grain_to_date

        def through_period(day: exp.Expression) -> exp.Expression:
            start = _func("DATE_TRUNC", exp.Literal.string(grain.value), day)
            ahead = adapter.date_add(exp.Cast(this=start, to=exp.DataType.build("DATE")), 1, grain)
            return adapter.date_add(ahead, -1, DurationUnit.DAY)

        return through_period
    return None


def _reaggregate(measure_expr: str) -> type[exp.AggFunc]:
    """How daily values of the base measure combine: a sum and a count add up, min and max nest."""
    parsed = _parse(measure_expr)
    agg = parsed if isinstance(parsed, exp.AggFunc) else parsed.find(exp.AggFunc)
    if isinstance(agg, exp.Min):
        return exp.Min
    if isinstance(agg, exp.Max):
        return exp.Max
    return exp.Sum


def _build_windowed(
    inputs: LeafInputs,
    leaf_metric: LeafMetric,
    *,
    order_index: int,
    order_column: tuple[str, str],
    column_type: NormalizedType,
    frame: CumulativeBinding,
    visibility: Sequence[exp.Expression],
    anchor: exp.Expression,
    reaggregate: type[exp.AggFunc],
) -> tuple[exp.Expression, tuple[AuxCte, ...]]:
    prefix = inputs.name_prefix or "_"
    daily, digits, bounds, spine, targets, windows = (
        f"{prefix}{suffix}"
        for suffix in ("daily", "digits", "bounds", "spine", "targets", "windows")
    )
    order_alias, order_dim = inputs.dimensions[order_index]
    partitions = [i for i in range(len(inputs.dimensions)) if i != order_index]

    day_dim = Dimension(
        name=DAY_COLUMN, expr=f"CAST({order_column[1]} AS DATE)", type=NormalizedType.DATE
    )
    daily_dims = [*(inputs.dimensions[i] for i in partitions), (order_alias, day_dim)]
    _enforce_safety_floor(
        [leaf_metric.resolved],
        fanout=inputs.fanout,
        grouped={dim.name for _alias, dim in daily_dims},
    )
    emit = _build_deduped if inputs.fanout else _build_simple
    daily_select = emit(
        inputs.owner,
        [leaf_metric.resolved],
        daily_dims,
        inputs.where_conditions,
        inputs.join_edges,
        inputs.ctx.sources_by_name,
        measure_aliases=[_VALUE],
        dim_mask=inputs.dim_mask,
    )
    daily_names = dict(zip(partitions, _dimension_output_names(daily_dims), strict=False))

    adapter = _active_adapter()
    spine_day = cast("exp.Expression", exp.column(DAY_COLUMN, table=spine))
    day_as_column = (
        spine_day
        if column_type is NormalizedType.DATE
        else exp.Cast(
            this=spine_day,
            to=exp.DataType.build(
                adapter.map_type(NormalizedType.TIMESTAMP), dialect=adapter.dialect
            ),
        )
    )

    def on_day(node: exp.Expression) -> exp.Expression:
        if isinstance(node, exp.Column) and (node.table, node.name) == order_column:
            return day_as_column.copy()
        return node

    bucket = _dimension_expr(
        order_alias, order_dim, _dim_mask_strategy(inputs.dim_mask, order_alias, order_dim)
    ).transform(on_day)
    target_select = exp.Select().from_(exp.to_table(spine))
    if visibility:
        target_select = target_select.where(
            exp.and_(*(condition.copy().transform(on_day) for condition in visibility))
        )
    if frame.period_agg is PeriodAgg.AVERAGE:
        target_select = target_select.select(_alias(bucket, _BUCKET), _alias(spine_day, DAY_COLUMN))
    else:
        pick = exp.Max if frame.period_agg is PeriodAgg.LAST else exp.Min
        target_select = target_select.select(
            _alias(bucket, _BUCKET),
            _alias(cast("exp.Expression", pick(this=spine_day.copy())), DAY_COLUMN),
        ).group_by(bucket.copy())

    target_day = cast("exp.Expression", exp.column(DAY_COLUMN, table=_TARGET))
    daily_day = cast("exp.Expression", exp.column(DAY_COLUMN, table=_DAILY))
    conditions: list[exp.Expression] = [exp.LTE(this=daily_day, expression=target_day)]
    if frame.window is not None:
        conditions.append(
            exp.GT(
                this=daily_day.copy(),
                expression=adapter.date_add(
                    target_day.copy(), -frame.window.count, frame.window.unit
                ),
            )
        )
    elif frame.grain_to_date is not None:
        period_start = exp.Cast(
            this=_func(
                "DATE_TRUNC", exp.Literal.string(frame.grain_to_date.value), target_day.copy()
            ),
            to=exp.DataType.build("DATE"),
        )
        conditions.append(exp.GTE(this=daily_day.copy(), expression=period_start))

    def joined(*columns: exp.Expression) -> exp.Select:
        return (
            exp.Select()
            .select(*columns)
            .from_(_alias(exp.to_table(targets), _TARGET))
            .join(
                _alias(exp.to_table(daily), _DAILY),
                on=exp.and_(*conditions),
                join_type="INNER",
            )
        )

    def partition_column(table: str, i: int) -> exp.Expression:
        return cast("exp.Expression", exp.column(daily_names[i], table=table))

    window_value = cast("exp.Expression", reaggregate(this=exp.column(_VALUE, table=_DAILY)))
    aux = [
        AuxCte(name=daily, body=daily_select),
        AuxCte(name=digits, body=digits_select()),
        AuxCte(name=bounds, body=bounds_select(daily, anchor, _reach(frame))),
        AuxCte(name=spine, body=spine_select(bounds, digits)),
        AuxCte(name=targets, body=target_select),
    ]
    output_names = inputs.dim_names
    if frame.period_agg is PeriodAgg.AVERAGE:
        target_bucket = cast("exp.Expression", exp.column(_BUCKET, table=_TARGET))
        group = [target_bucket, target_day, *(partition_column(_DAILY, i) for i in partitions)]
        per_day = joined(
            _alias(target_bucket.copy(), _BUCKET),
            _alias(target_day.copy(), DAY_COLUMN),
            *(_alias(partition_column(_DAILY, i), daily_names[i]) for i in partitions),
            _alias(window_value, _VALUE),
        ).group_by(*(g.copy() for g in group))
        aux.append(AuxCte(name=windows, body=per_day))
        source, bucket_column = _WINDOW, cast("exp.Expression", exp.column(_BUCKET, table=_WINDOW))
        value: exp.Expression = cast(
            "exp.Expression", exp.Avg(this=exp.column(_VALUE, table=_WINDOW))
        )
    else:
        source, bucket_column = _DAILY, cast("exp.Expression", exp.column(_BUCKET, table=_TARGET))
        value = window_value

    projections = [
        _alias(bucket_column.copy() if i == order_index else partition_column(source, i), name)
        for i, name in enumerate(output_names)
    ]
    projections.append(_alias(value, leaf_metric.alias or leaf_metric.resolved.measure.name))
    group_by = [bucket_column, *(partition_column(source, i) for i in partitions)]
    if frame.period_agg is PeriodAgg.AVERAGE:
        select = (
            exp.Select()
            .select(*projections)
            .from_(_alias(exp.to_table(windows), _WINDOW))
            .group_by(*(g.copy() for g in group_by))
        )
    else:
        select = joined(*projections).group_by(*(g.copy() for g in group_by))
    return select, tuple(aux)
