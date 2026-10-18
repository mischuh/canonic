"""Probe the latest period a query that only filters on a time dimension reaches."""

from __future__ import annotations

import dataclasses
from typing import TYPE_CHECKING

import sqlglot
from sqlglot import exp

from canonic.compiler._helpers import _measure_expr, _ResolvedMetric
from canonic.compiler.compose import Combine, LeafRef, MetricPlan
from canonic.compiler.leaf import LeafMetric, LeafPlan, _render
from canonic.compiler.result import PROBE_LATEST_PERIOD, ProbeColumn
from canonic.semantic.models import Additivity, Dimension, Measure

if TYPE_CHECKING:
    from collections.abc import Sequence

    from canonic.compiler.query import SemanticQuery
    from canonic.semantic.models import SemanticSource

__all__ = ["latest_period_probes", "latest_period_column"]

_PREFIX = "_canonic_latest_period_"


def latest_period_column(dimension: str) -> str:
    """The name of the hidden column that carries the latest value of ``dimension``."""
    return f"{_PREFIX}{dimension}"


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

    Only a dimension declared on the metric's own source is probed, and only on a plain
    additive leaf. Anything else is left unchecked, as before.
    """
    anchor = next(
        (leaf for leaf in leaves if leaf.key.strategy == "single" and leaf.finality is None),
        None,
    )
    if anchor is None or not query.filters:
        return [], [], []
    owner = sources_by_name.get(anchor.key.source)
    if owner is None:
        return [], [], []
    names = _filtered_names(query.filters)
    probe_leaves: list[LeafPlan] = []
    plans: list[MetricPlan] = []
    probes: list[ProbeColumn] = []
    for dimension in owner.dimensions:
        grain = dimension.period_grain
        expression = _max_expression(dimension)
        if (
            grain is None
            or expression is None
            or dimension.name in grouped
            or not ({dimension.name, dimension.column or ""} & names)
        ):
            continue
        column = latest_period_column(dimension.name)
        measure = Measure(name=column, expr=expression, additivity=Additivity.ADDITIVE)
        metric = LeafMetric(
            resolved=_ResolvedMetric(name=column, source=owner.name, measure=measure),
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
                    measures=((column, _render(_measure_expr(owner.name, measure)), column),),
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
                column=column, kind=PROBE_LATEST_PERIOD, subject=dimension.name, grain=grain
            )
        )
    return probe_leaves, plans, probes


def _max_expression(dimension: Dimension) -> str | None:
    """``MAX`` over the dimension's value, or ``None`` for a JSON key, which is not probed."""
    if dimension.json_path is not None:
        return None
    if dimension.expr is not None:
        return f"max({dimension.expr})"
    if dimension.column is None:
        return None
    return exp.Max(this=exp.column(dimension.column)).sql(dialect="postgres", identify=True)


def _filtered_names(filters: Sequence[str]) -> set[str]:
    """Every bare name the query's filters read."""
    names: set[str] = set()
    for text in filters:
        try:
            parsed = sqlglot.parse_one(text)
        except sqlglot.errors.ParseError:
            continue
        names.update(column.name for column in parsed.find_all(exp.Column))
    return names
