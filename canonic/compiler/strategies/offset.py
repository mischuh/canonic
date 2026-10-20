"""Offset compile path: another metric read shifted back along one time dimension.

The base is planned by its own kind under a :class:`~canonic.compiler._helpers.TimeShift`, so
every leaf it plans reads the time column moved forward by the offset in its dimensions and
query filters. The value in output period ``P`` is the base over the rows whose shifted date
falls in ``P``. Population filters, guardrails, tenant predicates and the finality watermark
are built on the stored column, since they describe rows rather than periods.
"""

from __future__ import annotations

import dataclasses
from datetime import timedelta
from typing import TYPE_CHECKING

from sqlglot import exp

from canonic.compiler._helpers import (
    TimeShift,
    _bind_filters,
    _find_dimension,
    _resolve_dimensions,
    enclosing_population,
    time_shift,
)
from canonic.compiler.joins import build_alias_tree
from canonic.compiler.result import OffsetMetadata
from canonic.semantic.duration import add_to_date

if TYPE_CHECKING:
    from collections.abc import Callable

    from canonic.compiler.compose import MetricLeaves
    from canonic.compiler.query import SemanticQuery
    from canonic.contracts.principal import EffectivePolicy
    from canonic.contracts.resolver import Binding as ResolverBinding
    from canonic.contracts.resolver import OffsetBinding
    from canonic.semantic.models import SemanticSource

__all__ = ["plan_metric"]

#: How far the unshifted bounds of a ``time_range`` are widened on each side. Month arithmetic
#: clamps to the end of the month, so moving a bound back by the offset is not an exact inverse
#: of moving a row forward. The shifted predicate stays exact, this one only narrows the scan.
_PREFILTER_MARGIN = timedelta(days=3)

_NO_EFFECT = (
    "offset metric {metric!r} has no effect here: the query neither groups nor filters by "
    "{dimension!r}, so the total over all rows is the base metric's"
)


def plan_metric(
    query: SemanticQuery,
    queried_name: str,
    binding: ResolverBinding,
    sources_by_name: dict[str, SemanticSource],
    *,
    effective_policy: EffectivePolicy,
    plan_base: Callable[[str, ResolverBinding], MetricLeaves],
) -> MetricLeaves:
    """Plan the base under the offset, and present its value under the offset's name."""
    assert binding.offset is not None  # noqa: S101 — routing guarantees offset kind
    offset = binding.offset
    owner = next(leaf.source for leaf in offset.base.leaves() if leaf.source is not None)
    shifted_filters, groups_by_it = _what_moves(
        query, offset, owner, sources_by_name, effective_policy
    )
    shift = TimeShift(
        time_dimension=offset.time_dimension,
        offset=offset.offset,
        prefilters=_prefilters(query, offset, owner, sources_by_name),
    )
    with time_shift(shift), enclosing_population(binding.binding.canonical.population_filter):
        planned = plan_base(offset.base.metric, offset.base)

    warnings = planned.warnings
    if not shifted_filters and not groups_by_it:
        warnings = (
            *warnings,
            _NO_EFFECT.format(metric=queried_name, dimension=offset.time_dimension),
        )
    return dataclasses.replace(
        planned,
        metric=dataclasses.replace(planned.metric, name=queried_name),
        resolved=binding.resolved_key or queried_name,
        offsets=(
            OffsetMetadata(
                metric=queried_name,
                base=offset.base.metric,
                time_dimension=offset.time_dimension,
                offset=str(offset.offset),
                shifted_filters=shifted_filters,
            ),
            *planned.offsets,
        ),
        warnings=warnings,
    )


def _shifted_column(
    offset: OffsetBinding,
    owner: str,
    sources_by_name: dict[str, SemanticSource],
    alias_to_source: dict[str, str],
) -> set[tuple[str, str]]:
    found = _find_dimension(offset.time_dimension, sources_by_name, owner, alias_to_source)
    if found is None:
        return set()
    alias, dim = found
    return {(alias, column) for column in dim.backing_columns()}


def _what_moves(
    query: SemanticQuery,
    offset: OffsetBinding,
    owner: str,
    sources_by_name: dict[str, SemanticSource],
    effective_policy: EffectivePolicy,
) -> tuple[list[str], bool]:
    """The query filters read on the shifted column, and whether a requested dimension is.

    Read against the first source the base reads, which is the source a caller thinks of.
    """
    alias_to_source = build_alias_tree(owner, sources_by_name)
    shifted = _shifted_column(offset, owner, sources_by_name, alias_to_source)
    filters: list[str] = []
    for raw in query.filters:
        (bound,), _ = _bind_filters(
            [raw], sources_by_name, owner, alias_to_source, effective_policy
        )
        if {(c.table, c.name) for c in bound.find_all(exp.Column)} & shifted:
            filters.append(raw)
    dimensions = _resolve_dimensions(query, sources_by_name, owner, alias_to_source)
    groups_by_it = any(
        {(alias, column) for column in dim.backing_columns()} & shifted for alias, dim in dimensions
    )
    return filters, groups_by_it


def _prefilters(
    query: SemanticQuery,
    offset: OffsetBinding,
    owner: str,
    sources_by_name: dict[str, SemanticSource],
) -> tuple[str, ...]:
    """A ``time_range`` on the shifted column, as bounds on the stored column.

    The range is evaluated on the shifted column, which no index covers. These bounds, moved
    back by the offset and widened by a few days, keep the scan as narrow as the range.
    """
    time_range = query.time_range
    if time_range is None:
        return ()
    alias_to_source = build_alias_tree(owner, sources_by_name)
    on_range = _find_dimension(time_range.dimension, sources_by_name, owner, alias_to_source)
    shifted = _shifted_column(offset, owner, sources_by_name, alias_to_source)
    if on_range is None or not (
        {(on_range[0], column) for column in on_range[1].backing_columns()} & shifted
    ):
        return ()
    bounds: list[str] = []
    if time_range.start is not None:
        start = add_to_date(time_range.start, offset.offset, sign=-1) - _PREFILTER_MARGIN
        bounds.append(f"{time_range.dimension} >= '{start.isoformat()}'")
    if time_range.end is not None:
        end = add_to_date(time_range.end, offset.offset, sign=-1) + _PREFILTER_MARGIN
        bounds.append(f"{time_range.dimension} < '{end.isoformat()}'")
    return tuple(bounds)
