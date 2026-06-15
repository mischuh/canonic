"""The deterministic compiler pipeline (SPEC-E5-E15 §4, stages 1–4, 6–8).

``compile`` turns a :class:`SemanticQuery` into dialect-correct, read-only SQL plus
result metadata. No LLM, no wall-clock, no randomness: identical inputs yield
byte-identical SQL (SPEC §8). The :class:`ContractResolver` is the only authority on
canonicality — the compiler trusts its results and never reimplements them (§6).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

import sqlglot
from sqlglot import exp

from canon.compiler.dialect import DIALECT_ADAPTERS
from canon.compiler.joins import JoinEdge, plan_joins
from canon.compiler.result import CompileResult, FiredGuardrail, SourceFreshness
from canon.contracts.resolver import Ambiguous as ResolverAmbiguous
from canon.contracts.resolver import Binding as ResolverBinding
from canon.contracts.resolver import Unresolved as ResolverUnresolved
from canon.exc import Ambiguous, Unresolved, UnsupportedMeasure
from canon.exc import Unreachable as UnreachableError
from canon.semantic.models import Relationship

if TYPE_CHECKING:
    from canon.compiler.query import SemanticQuery
    from canon.contracts.resolver import ContractResolver
    from canon.semantic.models import Dimension, Measure, SemanticSource

__all__ = ["compile"]

_DEDUP_ALIAS = "_base"
_FANOUT = frozenset({Relationship.ONE_TO_MANY, Relationship.MANY_TO_MANY})


# sqlglot builds many node classes dynamically, so mypy cannot see their inheritance
# from ``exp.Expression``. These thin wrappers cast at the boundary so the rest of the
# module stays strictly typed.
def _parse(sql: str) -> exp.Expression:
    return cast("exp.Expression", sqlglot.parse_one(sql))


def _alias(expr: exp.Expression, name: str) -> exp.Expression:
    return cast("exp.Expression", exp.alias_(expr, name))


def _func(name: str, *args: exp.Expression) -> exp.Expression:
    return cast("exp.Expression", exp.func(name, *args))


class _ResolvedMetric:
    """A metric request bound to its canonical source and measure (stage 1)."""

    __slots__ = ("measure", "name", "source")

    def __init__(self, name: str, source: str, measure: Measure) -> None:
        self.name = name
        self.source = source
        self.measure = measure


def compile(  # noqa: A001 — the public verb for this capability is "compile"
    query: SemanticQuery,
    resolver: ContractResolver,
    sources: list[SemanticSource],
) -> CompileResult:
    """Compile a semantic query to read-only SQL and result metadata (SPEC §4)."""
    sources_by_name = {s.name: s for s in sources}

    # Stage 1 — resolve metrics to (source, measure) via the canonicality authority.
    metrics = _resolve_metrics(query, resolver, sources_by_name)
    owner = metrics[0].source  # FROM anchor

    # Stage 2 — resolve dimensions & filters to owning sources.
    dimensions = _resolve_dimensions(query, sources_by_name)
    referenced = {src for src, _ in dimensions}
    where_conditions, filter_sources = _bind_filters(query.filters, sources_by_name)
    referenced |= filter_sources
    referenced |= {m.source for m in metrics}

    # Stage 3 — plan the join graph from the owner to every referenced source.
    join_edges = plan_joins(owner, referenced - {owner}, sources_by_name)

    # Stage 4 — fanout analysis: every measure must be P0-compilable; dedup additive
    # measures when a planned join fans out the grain.
    for m in metrics:
        if not m.measure.is_p0_compilable:
            raise UnsupportedMeasure(
                f"measure {m.source}.{m.measure.name!r} is not additive; "
                f"non-additive/semi-additive measures are deferred to P1"
            )
    fanout = any(edge.join.relationship in _FANOUT for edge in join_edges)

    # Stage 6 — enforce guardrails: AND mandatory filters into WHERE.
    guard_conditions, fired = _enforce_guardrails(metrics, resolver, query.context, sources_by_name)
    where_conditions += guard_conditions

    # Stage 7 — emit SQL through the dialect adapter.
    if fanout:
        ast = _build_deduped(
            owner, metrics, dimensions, where_conditions, join_edges, sources_by_name
        )
    else:
        ast = _build_simple(
            owner, metrics, dimensions, where_conditions, join_edges, sources_by_name
        )
    adapter = DIALECT_ADAPTERS["postgres"]
    sql = adapter.emit(ast, limit=query.limit)

    # Stage 8 — attach result metadata.
    used_sources = sorted({owner, *referenced, *{e.join.to for e in join_edges}})
    return CompileResult(
        sql=sql,
        dialect=adapter.dialect,
        resolved={m.name: f"{m.source}.{m.measure.name}" for m in metrics},
        guardrails_fired=fired,
        freshness=[_freshness(sources_by_name[s]) for s in used_sources],
        warnings=[],
    )


# --- Stage 1 -----------------------------------------------------------------


def _resolve_metrics(
    query: SemanticQuery,
    resolver: ContractResolver,
    sources_by_name: dict[str, SemanticSource],
) -> list[_ResolvedMetric]:
    if not query.metrics:
        raise Unresolved("query requests at least one metric")
    resolved: list[_ResolvedMetric] = []
    for name in query.metrics:
        result = resolver.resolve_metric(name, query.context)
        if isinstance(result, ResolverUnresolved):
            raise Unresolved(f"metric {name!r} matches no active binding")
        if isinstance(result, ResolverAmbiguous):
            raise Ambiguous(
                f"metric {name!r} matches more than one active binding",
                candidates=list(result.candidates),
            )
        assert isinstance(result, ResolverBinding)  # noqa: S101 — exhaustive over the union
        source = sources_by_name.get(result.source)
        if source is None:
            raise Unresolved(f"metric {name!r} binds to unknown source {result.source!r}")
        measure = _find_measure(source, result.measure)
        if measure is None:
            raise Unresolved(
                f"metric {name!r} binds to unknown measure {result.source}.{result.measure!r}"
            )
        resolved.append(_ResolvedMetric(name=name, source=result.source, measure=measure))
    return resolved


# --- Stage 2 -----------------------------------------------------------------


def _resolve_dimensions(
    query: SemanticQuery,
    sources_by_name: dict[str, SemanticSource],
) -> list[tuple[str, Dimension]]:
    resolved: list[tuple[str, Dimension]] = []
    for name in query.dimensions:
        found = _find_dimension(name, sources_by_name)
        if found is None:
            raise UnreachableError(f"dimension {name!r} is not declared on any source")
        resolved.append(found)
    return resolved


def _bind_filters(
    filters: list[str],
    sources_by_name: dict[str, SemanticSource],
) -> tuple[list[exp.Expression], set[str]]:
    """Parse filter strings, qualify referenced names to their owning source alias."""
    conditions: list[exp.Expression] = []
    used: set[str] = set()
    for raw in filters:
        parsed = _parse(raw)
        bound, sources = _qualify_columns(parsed, sources_by_name)
        conditions.append(bound)
        used |= sources
    return conditions, used


# --- Stage 6 -----------------------------------------------------------------


def _enforce_guardrails(
    metrics: list[_ResolvedMetric],
    resolver: ContractResolver,
    context: str | None,
    sources_by_name: dict[str, SemanticSource],
) -> tuple[list[exp.Expression], list[FiredGuardrail]]:
    conditions: list[exp.Expression] = []
    fired: list[FiredGuardrail] = []
    seen: set[str] = set()
    for m in metrics:
        for guardrail in resolver.guardrails_for(m.source, m.measure.name, context):
            if guardrail.id in seen:
                continue
            seen.add(guardrail.id)
            if guardrail.filter:
                parsed = _parse(guardrail.filter)
                bound, _ = _qualify_columns(parsed, sources_by_name)
                conditions.append(bound)
            fired.append(FiredGuardrail(id=guardrail.id, kind=str(guardrail.kind)))
    return conditions, fired


# --- Stage 7 helpers ---------------------------------------------------------


def _dimension_expr(source: str, dim: Dimension) -> exp.Expression:
    """Build the SELECT/GROUP-BY expression for a dimension (with time bucketing)."""
    col = exp.column(dim.column, table=source)
    if dim.granularity:
        return _func("DATE_TRUNC", exp.Literal.string(dim.granularity), col)
    return col


def _measure_expr(source: str, measure: Measure) -> exp.Expression:
    """Parse a measure expression and qualify its bare columns to the source alias."""
    parsed = _parse(measure.expr)
    return _qualify_to(parsed, source)


def _from_and_joins(
    select: exp.Select,
    owner: str,
    join_edges: list[JoinEdge],
    sources_by_name: dict[str, SemanticSource],
) -> exp.Select:
    owner_table = sources_by_name[owner].table
    select = select.from_(_alias(exp.to_table(owner_table), owner))
    for edge in join_edges:
        target = sources_by_name[edge.join.to]
        on_ast = _parse(edge.join.on)
        select = select.join(
            _alias(exp.to_table(target.table), edge.join.to),
            on=on_ast,
            join_type="LEFT",
        )
    return select


def _build_simple(
    owner: str,
    metrics: list[_ResolvedMetric],
    dimensions: list[tuple[str, Dimension]],
    where_conditions: list[exp.Expression],
    join_edges: list[JoinEdge],
    sources_by_name: dict[str, SemanticSource],
) -> exp.Select:
    """Single-SELECT emission for the no-fanout case (SPEC §4 step 7)."""
    select = exp.Select()
    projections: list[exp.Expression] = []
    group_exprs: list[exp.Expression] = []
    for src, dim in dimensions:
        expr = _dimension_expr(src, dim)
        projections.append(_alias(expr, dim.name))
        group_exprs.append(expr)
    for m in metrics:
        projections.append(_alias(_measure_expr(m.source, m.measure), m.measure.name))
    select = select.select(*projections)
    select = _from_and_joins(select, owner, join_edges, sources_by_name)
    if where_conditions:
        select = select.where(exp.and_(*where_conditions))
    if group_exprs:
        select = select.group_by(*group_exprs)
    return select


def _build_deduped(
    owner: str,
    metrics: list[_ResolvedMetric],
    dimensions: list[tuple[str, Dimension]],
    where_conditions: list[exp.Expression],
    join_edges: list[JoinEdge],
    sources_by_name: dict[str, SemanticSource],
) -> exp.Select:
    """Fanout-safe emission: dedup the measure grain in an inner ``DISTINCT ON`` subquery.

    A one→many / many→many join multiplies the measure source's rows; aggregating
    directly would inflate an additive sum. The inner query keeps one row per grain
    (Postgres ``DISTINCT ON``); the outer query aggregates over it (SPEC §4 step 4, S3 AC1).
    """
    owner_source = sources_by_name[owner]
    grain_cols = [exp.column(g, table=owner) for g in owner_source.grain]

    # Inner: DISTINCT ON (grain) projecting dimensions + each measure's input columns.
    inner = exp.Select()
    inner_projections: list[exp.Expression] = []
    measure_inputs: dict[str, exp.Expression] = {}
    for src, dim in dimensions:
        inner_projections.append(_alias(_dimension_expr(src, dim), dim.name))
    for m in metrics:
        for input_col in _input_columns(m.measure):
            measure_inputs.setdefault(input_col, exp.column(input_col, table=m.source))
    for col_name in sorted(measure_inputs):
        inner_projections.append(_alias(measure_inputs[col_name], col_name))
    inner = inner.select(*inner_projections).distinct(*grain_cols)
    inner = _from_and_joins(inner, owner, join_edges, sources_by_name)
    if where_conditions:
        inner = inner.where(exp.and_(*where_conditions))

    # Outer: aggregate the deduped rows.
    outer = exp.Select()
    projections: list[exp.Expression] = []
    group_exprs: list[exp.Expression] = []
    for _src, dim in dimensions:
        dim_col = exp.column(dim.name, table=_DEDUP_ALIAS)
        projections.append(_alias(dim_col, dim.name))
        group_exprs.append(dim_col)
    for m in metrics:
        projections.append(
            _alias(_qualify_to(_parse(m.measure.expr), _DEDUP_ALIAS), m.measure.name)
        )
    outer = outer.select(*projections).from_(_alias(inner.subquery(), _DEDUP_ALIAS))
    if group_exprs:
        outer = outer.group_by(*group_exprs)
    return outer


# --- shared helpers ----------------------------------------------------------


def _find_measure(source: SemanticSource, name: str) -> Measure | None:
    return next((m for m in source.measures if m.name == name), None)


def _find_dimension(
    name: str,
    sources_by_name: dict[str, SemanticSource],
) -> tuple[str, Dimension] | None:
    for src in sorted(sources_by_name):  # deterministic across sources
        for dim in sources_by_name[src].dimensions:
            if dim.name == name:
                return src, dim
    return None


def _bind_name(
    name: str,
    sources_by_name: dict[str, SemanticSource],
) -> tuple[str, str] | None:
    """Resolve a bare name to ``(source, physical_column)`` — dimension first, then column."""
    dim = _find_dimension(name, sources_by_name)
    if dim is not None:
        return dim[0], dim[1].column
    for src in sorted(sources_by_name):
        if any(c.name == name for c in sources_by_name[src].columns):
            return src, name
    return None


def _qualify_columns(
    expr: exp.Expression,
    sources_by_name: dict[str, SemanticSource],
) -> tuple[exp.Expression, set[str]]:
    """Qualify each bare column in a filter to its owning source alias + physical column."""
    used: set[str] = set()

    def transform(node: exp.Expression) -> exp.Expression:
        if isinstance(node, exp.Column):
            if node.table:
                used.add(node.table)
                return node
            binding = _bind_name(node.name, sources_by_name)
            if binding is None:
                raise UnreachableError(f"filter references unknown name {node.name!r}")
            src, col = binding
            used.add(src)
            return exp.column(col, table=src)
        return node

    return expr.transform(transform), used


def _qualify_to(expr: exp.Expression, alias: str) -> exp.Expression:
    """Qualify every bare column in an expression to a single source alias."""

    def transform(node: exp.Expression) -> exp.Expression:
        if isinstance(node, exp.Column) and not node.table:
            return exp.column(node.name, table=alias)
        return node

    return expr.transform(transform)


def _input_columns(measure: Measure) -> list[str]:
    """The column names a measure expression reads (e.g. ``amount`` for ``sum(amount)``)."""
    parsed = _parse(measure.expr)
    return sorted({c.name for c in parsed.find_all(exp.Column)})


def _freshness(source: SemanticSource) -> SourceFreshness:
    last = source.meta.last_validated_at
    return SourceFreshness(
        source=source.name,
        last_validated_at=last.isoformat() if last is not None else None,
        stale=False,
    )
