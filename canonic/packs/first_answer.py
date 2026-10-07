"""§5.6: run the pack's declared first-answer query right after install.

Reuses the exact production query path — ``SemanticQuery`` compiled and executed by
``CanonicService`` — the same way ``canonic query`` does. No new query path; the only
pack-specific behavior here is turning ``first_answer.window`` into a bounded filter on
whichever day-granularity date/timestamp dimension the metric's source declares.
"""

from __future__ import annotations

import asyncio
import re
from datetime import date, timedelta
from typing import TYPE_CHECKING

from canonic.compiler.query import SemanticQuery, parse_filter_flag
from canonic.contracts.loader import load_metric_bindings
from canonic.core.service import CanonicService
from canonic.exc import Unresolved
from canonic.semantic.loader import list_semantic_sources
from canonic.semantic.models import NormalizedType

if TYPE_CHECKING:
    from pathlib import Path

    from canonic.contracts.models import MetricBinding
    from canonic.core.models import QueryResult
    from canonic.packs.manifest import FirstAnswer
    from canonic.semantic.models import Dimension, SemanticSource

__all__ = ["FirstAnswerOutcome", "run_first_answer"]

_WINDOW_RE = re.compile(r"^(\d+)d$")
_DATE_DIM_TYPES = frozenset({NormalizedType.DATE, NormalizedType.TIMESTAMP})


class FirstAnswerOutcome:
    """The rendered pieces of a first-answer run: rows, query, source and one-line definition."""

    def __init__(
        self, *, result: QueryResult, sq: SemanticQuery, source_name: str, definition: str
    ) -> None:
        self.result = result
        self.sq = sq
        self.source_name = source_name
        self.definition = definition


def run_first_answer(project_root: Path, spec: FirstAnswer) -> FirstAnswerOutcome:
    """Run ``spec.metric`` (windowed by ``spec.window`` when a day dimension exists).

    Raises the underlying ``CanonicError`` on failure — the caller decides what to do
    (§5.6: "If the query fails, the installed files stay in place").
    """
    bindings = load_metric_bindings(project_root)
    binding = next(
        (b for b in bindings if b.metric == spec.metric or spec.metric in b.aliases), None
    )
    if binding is None:
        raise Unresolved(f"first_answer metric {spec.metric!r} matches no active binding")

    sources = list_semantic_sources(project_root)
    source_name = binding.canonical.source
    filters: list[str] = []
    if spec.window and source_name is not None:
        since = _window_since(spec.window)
        source = next((s for s in sources if s.name == source_name), None)
        dim = _day_dimension(source) if source is not None else None
        if since is not None and dim is not None:
            filters.append(parse_filter_flag(f"{dim.name}:>=:{since.isoformat()}"))

    sq = SemanticQuery(metrics=[spec.metric], filters=filters, limit=10)
    service = CanonicService.from_project(project_root)
    result = asyncio.run(service.query(sq))
    return FirstAnswerOutcome(
        result=result,
        sq=sq,
        source_name=source_name or spec.metric,
        definition=_definition_line(binding, sources),
    )


def _definition_line(binding: MetricBinding, sources: list[SemanticSource]) -> str:
    """The metric's definition in one line, from its canonical binding (§5.6)."""
    ref = binding.canonical
    expr: str | None = None
    if ref.source is not None and ref.measure is not None:
        source = next((s for s in sources if s.name == ref.source), None)
        measure = (
            next((m for m in source.measures if m.name == ref.measure), None) if source else None
        )
        expr = measure.expr if measure is not None else ref.measure
    elif ref.numerator is not None and ref.denominator is not None:
        expr = f"{ref.numerator} / {ref.denominator}"
    elif ref.distinct_on is not None:
        expr = f"count(distinct {ref.distinct_on})"
    elif ref.column is not None and ref.quantile is not None:
        expr = f"percentile({ref.quantile:g}) of {ref.column}"
    definition = f"{binding.metric} = {expr}" if expr else f"{binding.metric} ({ref.kind.value})"
    if ref.source is not None:
        definition += f" on {ref.source}"
    if ref.population_filter:
        definition += f" where {ref.population_filter}"
    return definition


def _window_since(window: str) -> date | None:
    match = _WINDOW_RE.match(window.strip())
    if match is None:
        return None
    return date.today() - timedelta(days=int(match.group(1)))


def _day_dimension(source: SemanticSource) -> Dimension | None:
    for dim in source.dimensions:
        if dim.value_type(source.columns) in _DATE_DIM_TYPES and dim.granularity == "day":
            return dim
    return None
