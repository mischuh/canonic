"""The deterministic compiler pipeline (SPEC-E5-E15 §4, stages 1–4, 6–8).

``compile`` turns a :class:`SemanticQuery` into dialect-correct, read-only SQL plus
result metadata. No LLM, no wall-clock, no randomness: identical inputs yield
byte-identical SQL (SPEC §8). The :class:`ContractResolver` is the only authority on
canonicality — the compiler trusts its results and never reimplements them (§6).
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, cast

import sqlglot
from sqlglot import exp

from canon.compiler.dialect import DIALECT_ADAPTERS
from canon.compiler.joins import JoinEdge, plan_joins
from canon.compiler.result import CompileResult, FinalityMetadata, FiredGuardrail, SourceFreshness
from canon.contracts.resolver import Ambiguous as ResolverAmbiguous
from canon.contracts.resolver import Binding as ResolverBinding
from canon.contracts.resolver import Unresolved as ResolverUnresolved
from canon.exc import Ambiguous, GuardrailBlock, Unresolved, UnsupportedMeasure
from canon.exc import Unreachable as UnreachableError
from canon.semantic.models import NormalizedType, Relationship

if TYPE_CHECKING:
    from canon.compiler.query import SemanticQuery
    from canon.contracts.models import FinalityRule
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
    dimensions = _resolve_dimensions(query, sources_by_name, owner)
    referenced = {src for src, _ in dimensions}
    where_conditions, filter_sources = _bind_filters(query.filters, sources_by_name, owner)
    referenced |= filter_sources
    referenced |= {m.source for m in metrics}

    # Stage 3 — plan the join graph from the owner to every referenced source.
    join_edges = plan_joins(
        owner, referenced - {owner}, sources_by_name, via=list(query.via) or None
    )

    # Stage 4 — fanout analysis: every measure must be P0-compilable; dedup additive
    # measures when a planned join fans out the grain.
    for m in metrics:
        if not m.measure.is_p0_compilable:
            raise UnsupportedMeasure(
                f"measure {m.source}.{m.measure.name!r} is not additive; "
                f"non-additive/semi-additive measures are deferred to P1"
            )
    fanout = any(edge.join.relationship in _FANOUT for edge in join_edges)

    # Stage 5 — finality & coalescing [P1]: evaluate watermark, select sources per window.
    finality_rule = resolver.finality_for(metrics[0].name) if len(metrics) == 1 else None
    time_dim_name: str | None = None
    if finality_rule is not None:
        time_dim_name = _find_time_dim_name(dimensions, sources_by_name)
        if time_dim_name is None:
            finality_rule = None  # no time dimension → all rows implicitly final

    # Stage 5b — restrict_source: block queries that would pull provisional rows in guarded contexts.
    _enforce_restrict_source(query, metrics, resolver, finality_rule, sources_by_name)

    # Stage 6 — enforce guardrails: AND mandatory filters into WHERE.
    guard_conditions, fired = _enforce_guardrails(metrics, resolver, query.context, sources_by_name)
    where_conditions += guard_conditions

    # Stage 7 — emit SQL through the dialect adapter.
    finality_meta: FinalityMetadata | None = None
    adapter = DIALECT_ADAPTERS["postgres"]
    if finality_rule is not None and time_dim_name is not None:
        from canon.contracts.finality import evaluate_watermark, watermark_to_iso

        final_r = next(r for r in finality_rule.realizations if r.role == "final")
        watermark_dt = evaluate_watermark(
            cast("str", final_r.watermark), cast("str", final_r.tz), query.as_of
        )
        ast = _build_finality_union(
            rule=finality_rule,
            query_metrics=metrics,
            dimensions=dimensions,
            where_conditions=where_conditions,
            sources_by_name=sources_by_name,
            watermark_dt=watermark_dt,
            time_dim_name=time_dim_name,
            original_owner=owner,
        )
        finality_meta = FinalityMetadata(
            watermark=watermark_to_iso(watermark_dt),
            sources_used=[r.source for r in finality_rule.realizations],
            result_flag=finality_rule.result_flag or "per_row",
        )
    elif fanout:
        ast = _build_deduped(
            owner, metrics, dimensions, where_conditions, join_edges, sources_by_name
        )
    else:
        ast = _build_simple(
            owner, metrics, dimensions, where_conditions, join_edges, sources_by_name
        )
    sql = adapter.emit(ast, limit=query.limit)

    # Stage 8 — attach result metadata.
    if finality_meta is not None:
        used_sources = sorted(finality_meta.sources_used)
    else:
        used_sources = sorted({owner, *referenced, *{e.join.to for e in join_edges}})
    return CompileResult(
        sql=sql,
        dialect=adapter.dialect,
        resolved={m.name: f"{m.source}.{m.measure.name}" for m in metrics},
        guardrails_fired=fired,
        freshness=[_freshness(sources_by_name[s]) for s in used_sources],
        warnings=[],
        finality=finality_meta,
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
    owner: str,
) -> list[tuple[str, Dimension]]:
    resolved: list[tuple[str, Dimension]] = []
    for name in query.dimensions:
        found = _find_dimension(name, sources_by_name, owner)
        if found is None:
            raise UnreachableError(f"dimension {name!r} is not declared on any source")
        resolved.append(found)
    return resolved


def _bind_filters(
    filters: list[str],
    sources_by_name: dict[str, SemanticSource],
    owner: str,
) -> tuple[list[exp.Expression], set[str]]:
    """Parse filter strings, qualify referenced names to their owning source alias."""
    conditions: list[exp.Expression] = []
    used: set[str] = set()
    for raw in filters:
        parsed = _parse(raw)
        bound, sources = _qualify_columns(parsed, sources_by_name, owner)
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
                bound, _ = _qualify_columns(parsed, sources_by_name, m.source)
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


def _reachable_from(
    owner: str,
    sources_by_name: dict[str, SemanticSource],
) -> set[str]:
    """BFS over declared joins from owner; returns all transitively reachable source names.

    Does not include ``owner`` itself so callers can distinguish
    "on the owner" from "reachable via join" cleanly.
    """
    reachable: set[str] = set()
    frontier = [owner]
    visited: set[str] = {owner}
    while frontier:
        node = frontier.pop()
        source = sources_by_name.get(node)
        if source is None:
            continue
        for join in source.joins:
            if join.to not in visited:
                visited.add(join.to)
                reachable.add(join.to)
                frontier.append(join.to)
    return reachable


def _find_dimension(
    name: str,
    sources_by_name: dict[str, SemanticSource],
    owner: str | None = None,
) -> tuple[str, Dimension] | None:
    if owner is not None:
        # Priority 1: the metric's owning source wins unconditionally.
        owner_source = sources_by_name.get(owner)
        if owner_source is not None:
            for dim in owner_source.dimensions:
                if dim.name == name:
                    return owner, dim

        # Priority 2: search join-reachable sources for the dimension.
        reachable = _reachable_from(owner, sources_by_name)
        candidates: list[tuple[str, Dimension]] = []
        for src in sorted(reachable):
            for dim in sources_by_name[src].dimensions:
                if dim.name == name:
                    candidates.append((src, dim))
        if len(candidates) == 1:
            return candidates[0]
        if len(candidates) > 1:
            raise Ambiguous(
                f"dimension {name!r} is present on multiple join-reachable sources; "
                f"qualify explicitly",
                candidates=[src for src, _ in candidates],
            )
        return None

    # No owner: fall back to alphabetical scan (preserves behaviour for callers without context).
    for src in sorted(sources_by_name):
        for dim in sources_by_name[src].dimensions:
            if dim.name == name:
                return src, dim
    return None


def _bind_name(
    name: str,
    sources_by_name: dict[str, SemanticSource],
    owner: str | None = None,
) -> tuple[str, str] | None:
    """Resolve a bare name to ``(source, physical_column)`` — dimension first, then column."""
    dim = _find_dimension(name, sources_by_name, owner)
    if dim is not None:
        return dim[0], dim[1].column
    for src in sorted(sources_by_name):
        if any(c.name == name for c in sources_by_name[src].columns):
            return src, name
    return None


def _qualify_columns(
    expr: exp.Expression,
    sources_by_name: dict[str, SemanticSource],
    owner: str | None = None,
) -> tuple[exp.Expression, set[str]]:
    """Qualify each bare column in a filter to its owning source alias + physical column."""
    used: set[str] = set()

    def transform(node: exp.Expression) -> exp.Expression:
        if isinstance(node, exp.Column):
            if node.table:
                used.add(node.table)
                return node
            binding = _bind_name(node.name, sources_by_name, owner)
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


# --- Stage 5 helpers ---------------------------------------------------------


_TIME_TYPES = frozenset({NormalizedType.DATE, NormalizedType.TIMESTAMP})


def _find_time_dim_name(
    dimensions: list[tuple[str, Dimension]],
    sources_by_name: dict[str, SemanticSource],
) -> str | None:
    """Return the name of the first DATE/TIMESTAMP dimension in the query, or None."""
    for src_name, dim in dimensions:
        source = sources_by_name.get(src_name)
        if source is None:
            continue
        col = next((c for c in source.columns if c.name == dim.column), None)
        if col is not None and col.type in _TIME_TYPES:
            return dim.name
    return None


def _requalify_source(
    expr: exp.Expression,
    old_source: str,
    new_source: str,
) -> exp.Expression:
    """Replace every column table reference that equals old_source with new_source."""

    def _transform(node: exp.Expression) -> exp.Expression:
        if isinstance(node, exp.Column) and node.table == old_source:
            return exp.column(node.name, table=new_source)
        return node

    return expr.transform(_transform)


def _build_finality_union(
    rule: FinalityRule,
    query_metrics: list[_ResolvedMetric],
    dimensions: list[tuple[str, Dimension]],
    where_conditions: list[exp.Expression],
    sources_by_name: dict[str, SemanticSource],
    watermark_dt: datetime,
    time_dim_name: str,
    original_owner: str,
) -> exp.Expression:
    """Build a UNION ALL over finality realizations (SPEC-E5-E15 stage 5).

    Each branch selects from one realization source, gated by the watermark on the time
    dimension column, and projects an ``is_final`` boolean marker.  All WHERE conditions
    from the original owner (user filters + guardrails) are re-qualified to each branch
    source — this is valid because both sources share the same column/dimension schema.
    """
    from canon.contracts.finality import watermark_to_iso

    watermark_iso = watermark_to_iso(watermark_dt)
    watermark_lit = cast(
        "exp.Expression",
        exp.Cast(
            this=exp.Literal.string(watermark_iso),
            to=exp.DataType.build("TIMESTAMPTZ"),
        ),
    )

    branches: list[exp.Select] = []
    for realization in rule.realizations:
        src_name = realization.source
        source = sources_by_name.get(src_name)
        if source is None:
            raise Unresolved(
                f"finality realization source {src_name!r} is not in the loaded sources"
            )

        # Resolve the metric's measure on this realization source.
        branch_metrics: list[_ResolvedMetric] = []
        for m in query_metrics:
            measure = _find_measure(source, m.measure.name)
            if measure is None:
                raise Unresolved(
                    f"finality realization source {src_name!r} does not declare measure "
                    f"{m.measure.name!r} required by metric {m.name!r}"
                )
            branch_metrics.append(_ResolvedMetric(name=m.name, source=src_name, measure=measure))

        # Resolve dimensions on this realization source (same names must exist).
        branch_dims: list[tuple[str, Dimension]] = []
        for _orig, dim in dimensions:
            found = next((d for d in source.dimensions if d.name == dim.name), None)
            if found is None:
                raise UnreachableError(
                    f"finality realization source {src_name!r} does not declare "
                    f"dimension {dim.name!r}"
                )
            branch_dims.append((src_name, found))

        # Find the gate column (time dimension backing column on this source).
        gate_col: exp.Expression | None = None
        for _src, dim in branch_dims:
            if dim.name == time_dim_name:
                gate_col = exp.column(dim.column, table=src_name)
                break
        if gate_col is None:
            raise UnreachableError(
                f"could not resolve time dimension {time_dim_name!r} on source {src_name!r}"
            )

        # Build gate and is_final marker.
        if realization.role == "final":
            gate: exp.Expression = cast(
                "exp.Expression", exp.LTE(this=gate_col, expression=watermark_lit)
            )
            is_final_val: exp.Expression = cast("exp.Expression", exp.true())
        else:
            gate = cast("exp.Expression", exp.GT(this=gate_col, expression=watermark_lit))
            is_final_val = cast("exp.Expression", exp.false())

        # Re-qualify all shared WHERE conditions from original_owner to this branch source.
        branch_where = [
            _requalify_source(cond, original_owner, src_name) for cond in where_conditions
        ]
        branch_where.append(gate)

        # Build the branch SELECT.
        select = exp.Select()
        projections: list[exp.Expression] = []
        group_exprs: list[exp.Expression] = []
        for b_src, dim in branch_dims:
            expr = _dimension_expr(b_src, dim)
            projections.append(_alias(expr, dim.name))
            group_exprs.append(expr)
        for m in branch_metrics:
            projections.append(_alias(_measure_expr(m.source, m.measure), m.measure.name))
        projections.append(_alias(is_final_val, "is_final"))
        select = select.select(*projections)
        select = select.from_(_alias(exp.to_table(source.table), src_name))
        if branch_where:
            select = select.where(exp.and_(*branch_where))
        if group_exprs:
            select = select.group_by(*group_exprs)
        branches.append(select)

    if not branches:
        raise UnreachableError("finality rule has no realizations to build from")

    combined: exp.Select | exp.Union = branches[0]
    for branch in branches[1:]:
        combined = combined.union(branch, distinct=False)
    return combined


# --- Stage 5b helpers ---------------------------------------------------------


def _parse_datetime_literal(lit: str) -> datetime | None:
    """Try to parse an ISO date or datetime literal; return None if unparseable."""
    from datetime import UTC

    for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%d %H:%M:%S%z", "%Y-%m-%d"):
        try:
            dt = datetime.strptime(lit.strip("'\""), fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=UTC)
            return dt
        except ValueError:
            continue
    return None


def _time_column_names(
    finality_rule: FinalityRule,
    sources_by_name: dict[str, SemanticSource],
) -> frozenset[str]:
    """Return the set of physical column names that back the time dimension on any realization."""
    names: set[str] = set()
    for realization in finality_rule.realizations:
        source = sources_by_name.get(realization.source)
        if source is None:
            continue
        for dim in source.dimensions:
            col = next((c for c in source.columns if c.name == dim.column), None)
            if col is not None and col.type in _TIME_TYPES:
                names.add(dim.column)
                names.add(dim.name)
    return frozenset(names)


def _window_exceeds_watermark(
    filters: list[str],
    time_names: frozenset[str],
    watermark_dt: datetime,
    sources_by_name: dict[str, SemanticSource],
) -> bool:
    """Return True if the query window, as derived from time-dimension filters, exceeds watermark.

    Decision rule (per spec §2.4, confirmed):
    - No time predicate at all → False (allow; coalescing handles per-row finality).
    - Finite upper bound U → block iff U > watermark.
    - Open upper bound but finite lower bound L → block iff L > watermark.
    """
    upper: datetime | None = None  # minimum upper-bound literal found
    lower: datetime | None = None  # maximum lower-bound literal found
    found_any = False

    for raw in filters:
        try:
            parsed = _parse(raw)
        except Exception:  # noqa: BLE001
            continue
        for node in parsed.walk():
            # Determine if this comparison node touches a time column.
            if isinstance(node, (exp.LTE, exp.LT, exp.GTE, exp.GT, exp.EQ)):
                col_node = node.this if isinstance(node.this, exp.Column) else None
                lit_node = node.expression if isinstance(node.expression, exp.Literal) else None
                # Also handle reversed comparisons: literal op column
                if col_node is None and isinstance(node.this, exp.Literal):
                    lit_node = node.this
                    col_node = node.expression if isinstance(node.expression, exp.Column) else None
                if col_node is None or lit_node is None:
                    continue
                col_name = col_node.name
                if col_name not in time_names:
                    continue
                dt = _parse_datetime_literal(lit_node.this)
                if dt is None:
                    continue
                found_any = True
                if isinstance(node, (exp.LTE, exp.LT)):
                    upper = dt if upper is None else min(upper, dt)
                elif isinstance(node, (exp.GTE, exp.GT)):
                    lower = dt if lower is None else max(lower, dt)
                else:  # EQ
                    upper = dt if upper is None else min(upper, dt)
                    lower = dt if lower is None else max(lower, dt)
            elif isinstance(node, exp.Between):
                col_node = node.this if isinstance(node.this, exp.Column) else None
                if col_node is None or col_node.name not in time_names:
                    continue
                lo_node = node.args.get("low")
                hi_node = node.args.get("high")
                lo = (
                    _parse_datetime_literal(lo_node.this)
                    if isinstance(lo_node, exp.Literal)
                    else None
                )
                hi = (
                    _parse_datetime_literal(hi_node.this)
                    if isinstance(hi_node, exp.Literal)
                    else None
                )
                if lo is not None:
                    found_any = True
                    lower = lo if lower is None else max(lower, lo)
                if hi is not None:
                    found_any = True
                    upper = hi if upper is None else min(upper, hi)

    if not found_any:
        return False

    from datetime import UTC

    wm = watermark_dt
    if upper is not None:
        upper_utc = upper.astimezone(UTC)
        wm_utc = wm.astimezone(UTC)
        return upper_utc > wm_utc
    if lower is not None:
        lower_utc = lower.astimezone(UTC)
        wm_utc = wm.astimezone(UTC)
        return lower_utc > wm_utc
    return False


def _enforce_restrict_source(
    query: SemanticQuery,
    metrics: list[_ResolvedMetric],
    resolver: ContractResolver,
    finality_rule: FinalityRule | None,
    sources_by_name: dict[str, SemanticSource],
) -> None:
    """Stage 5b: raise GuardrailBlock if a restrict_source guardrail is violated (SPEC §2.4)."""
    if not query.context:
        return

    for m in metrics:
        for guardrail in resolver.restrict_source_for(m.source, m.measure.name, query.context):
            if guardrail.restrict_to is None or guardrail.restrict_to.role != "final":
                continue
            rule = finality_rule if finality_rule is not None else resolver.finality_for(m.name)
            if rule is None:
                continue
            final_r = next((r for r in rule.realizations if r.role == "final"), None)
            if final_r is None or not final_r.watermark or not final_r.tz:
                continue

            from canon.contracts.finality import evaluate_watermark

            watermark_dt = evaluate_watermark(final_r.watermark, final_r.tz, query.as_of)
            time_names = _time_column_names(rule, sources_by_name)
            if _window_exceeds_watermark(query.filters, time_names, watermark_dt, sources_by_name):
                raise GuardrailBlock(guardrail.rationale)
