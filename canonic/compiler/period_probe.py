"""Probe the latest period a query that only filters on a time dimension reaches."""

from __future__ import annotations

import dataclasses
import re
from typing import TYPE_CHECKING

import sqlglot
from sqlglot import exp

from canonic.compiler._helpers import (
    _bind_name,
    _find_dimension,
    _measure_expr,
    _parse,
    _qualify_to,
    _render_for_parse,
    _ResolvedMetric,
)
from canonic.compiler.compose import Combine, LeafRef, MetricPlan
from canonic.compiler.joins import build_alias_tree
from canonic.compiler.leaf import LeafMetric, LeafPlan, _render
from canonic.compiler.result import PROBE_LATEST_PERIOD, ProbeColumn
from canonic.exc import Ambiguous
from canonic.semantic.models import Additivity, Dimension, Measure

if TYPE_CHECKING:
    from collections.abc import Sequence

    from canonic.compiler.query import SemanticQuery
    from canonic.semantic.models import SemanticSource

__all__ = ["latest_period_column", "latest_period_probes"]

_PREFIX = "_canonic_latest_period_"


def latest_period_column(subject: str) -> str:
    """The name of the hidden column that carries the latest value of ``subject``."""
    return f"{_PREFIX}{subject.replace('.', '_')}"


def latest_period_probes(
    leaves: Sequence[LeafPlan],
    query: SemanticQuery,
    sources_by_name: dict[str, SemanticSource],
    grouped: set[str],
    *,
    first_index: int,
) -> tuple[list[LeafPlan], list[MetricPlan], list[ProbeColumn]]:
    """Extra leaves, metric plans and probe columns for the time dimensions a query filters on.

    A query that groups by a time dimension has its periods in the result. One that only
    filters on it does not, so a ``MAX`` of the dimension is projected from the same rows as the
    first metric: the probe leaf repeats that leaf's plan, so it fuses into the same CTE and
    sees exactly the rows the answer is computed from, guardrails and tenant scoping included.
    ``grouped`` names the time dimensions already read from the result, which need no probe.
    ``first_index`` is the index the first probe leaf will have in the final leaf list.

    A dimension is probed on a plain additive leaf, either on the metric's own source or on a
    joined source that does not multiply the metric's rows. A dimension behind a join that fans
    out, and one that reads a JSON key, are left unchecked. Neither is a dimension on the same
    column as a grouped time dimension: the result already shows its periods, and a filter on
    the shared column (``order_date`` under ``order_month``) would otherwise probe every other
    grain declared on it. A latest day that has not ended lies in a latest month that has not
    ended, and a month that has not ended says nothing about complete days.
    """
    anchor = next(
        (leaf for leaf in leaves if leaf.key.strategy == "single" and leaf.finality is None),
        None,
    )
    if anchor is None or not query.filters:
        return [], [], []
    owner = anchor.key.source
    alias_to_source = build_alias_tree(owner, sources_by_name)
    grouped_columns = _grouped_columns(query, grouped, owner, sources_by_name, alias_to_source)
    probe_leaves: list[LeafPlan] = []
    plans: list[MetricPlan] = []
    probes: list[ProbeColumn] = []
    for alias, dimension in _filtered_time_dimensions(
        query.filters, owner, sources_by_name, alias_to_source
    ):
        subject = dimension.name if alias == owner else f"{alias}.{dimension.name}"
        grain = dimension.period_grain
        expression = _max_expression(dimension, alias)
        time_format = dimension.period_format
        if (
            grain is None
            or expression is None
            # MAX over a label is its latest period only when the labels sort by time
            or (time_format is not None and not time_format.sorts_chronologically)
            or subject in grouped
            or dimension.name in grouped
            or (alias, dimension.column) in grouped_columns
            or (alias != owner and anchor.fanout)
        ):
            continue
        column = latest_period_column(subject)
        measure = Measure(name=column, expr=expression, additivity=Additivity.ADDITIVE)
        if not measure.is_p0_compilable:
            continue  # a probe must never turn a query that compiles into one that does not
        metric = LeafMetric(
            resolved=_ResolvedMetric(name=column, source=owner, measure=measure),
            population_filter=anchor.metrics[0].population_filter,
            alias=column,
        )
        probe_leaves.append(
            dataclasses.replace(
                anchor,
                metrics=(metric,),
                measure_aliases=(column,),
                key=dataclasses.replace(
                    anchor.key,
                    measures=((column, _render(_measure_expr(owner, measure)), column),),
                ),
                time_dimensions=(),
            )
        )
        plans.append(
            MetricPlan(
                name=column,
                refs=(LeafRef(leaf=first_index + len(probe_leaves) - 1, column=column),),
                combine=Combine.DIRECT,
            )
        )
        probes.append(
            ProbeColumn(
                column=column,
                kind=PROBE_LATEST_PERIOD,
                subject=subject,
                grain=grain,
                time_format=time_format,
            )
        )
    return probe_leaves, plans, probes


def _grouped_columns(
    query: SemanticQuery,
    grouped: set[str],
    owner: str,
    sources_by_name: dict[str, SemanticSource],
    alias_to_source: dict[str, str],
) -> set[tuple[str, str]]:
    """The ``(alias, column)`` pairs behind the grouped time dimensions that read a column."""
    columns: set[tuple[str, str]] = set()
    for name in query.dimensions:
        if name not in grouped and name.rsplit(".", 1)[-1] not in grouped:
            continue
        try:
            found = _find_dimension(name, sources_by_name, owner, alias_to_source)
        except Ambiguous:
            continue
        if found is not None and found[1].column is not None and found[1].json_path is None:
            columns.add((found[0], found[1].column))
    return columns


def _filtered_time_dimensions(
    filters: Sequence[str],
    owner: str,
    sources_by_name: dict[str, SemanticSource],
    alias_to_source: dict[str, str],
) -> list[tuple[str, Dimension]]:
    """The ``(alias, dimension)`` pairs with a known period that the filters read, in order.

    Names resolve the way the filter itself does: a dimension name first, then a column, which
    stands for the dimensions declared on that column. A name that matches a dimension without a
    known period counts for that dimension only.
    """
    found: dict[tuple[str, str], tuple[str, Dimension]] = {}
    for text in filters:
        try:
            parsed = sqlglot.parse_one(text)
        except sqlglot.errors.ParseError:
            continue
        for column in parsed.find_all(exp.Column):
            for alias, dimension in _dimensions_of(column, owner, sources_by_name, alias_to_source):
                if dimension.period_grain is not None:
                    found.setdefault((alias, dimension.name), (alias, dimension))
    return list(found.values())


def _dimensions_of(
    column: exp.Column,
    owner: str,
    sources_by_name: dict[str, SemanticSource],
    alias_to_source: dict[str, str],
) -> list[tuple[str, Dimension]]:
    if column.table:
        source = sources_by_name.get(alias_to_source.get(column.table, ""))
        if source is None:
            return []
        return [(column.table, d) for d in source.dimensions if d.column == column.name]
    try:
        named = _find_dimension(column.name, sources_by_name, owner, alias_to_source)
    except Ambiguous:
        return []
    if named is not None:
        return [named]
    bound = _bind_name(column.name, sources_by_name, owner, alias_to_source)
    if bound is None:
        return []
    alias, physical = bound
    source = sources_by_name.get(alias_to_source.get(alias, alias))
    if source is None:
        return []
    return [(alias, d) for d in source.dimensions if d.column == physical]


_SIMPLE_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _max_expression(dimension: Dimension, alias: str) -> str | None:
    """``MAX`` over the dimension's value on ``alias``, or ``None`` if it cannot be probed.

    The text is read back in the connection's dialect and, to check that it compiles, in the
    default one. A quoted name would be a string in some dialects, and a dialect's own quoting
    does not parse in the default one, so a name is written bare and qualified by its alias.
    Names that would need quoting, and JSON keys, are not probed.
    """
    if dimension.json_path is not None or not _SIMPLE_NAME.match(alias):
        return None
    if dimension.expr is not None:
        return _render_for_parse(exp.Max(this=_qualify_to(_parse(dimension.expr), alias)))
    if dimension.column is None or not _SIMPLE_NAME.match(dimension.column):
        return None
    return f"MAX({alias}.{dimension.column})"
