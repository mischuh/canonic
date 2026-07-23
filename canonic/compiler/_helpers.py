"""Shared stage helpers for the deterministic compiler pipeline (SPEC §4).

Cross-strategy plumbing: sqlglot/AST primitives, dimension and measure resolution,
guardrail enforcement, finality-union assembly, and freshness metadata. Imported by
both the router in :mod:`canonic.compiler.pipeline` and every per-kind strategy."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, cast

import sqlglot
from sqlglot import exp

from canonic.compiler.joins import JoinEdge, build_alias_tree, plan_joins
from canonic.compiler.result import (
    FiredGuardrail,
    SourceFreshness,
)
from canonic.exc import Ambiguous, Unresolved
from canonic.exc import Unreachable as UnreachableError
from canonic.semantic.models import Measure, NormalizedType, Relationship

if TYPE_CHECKING:
    from datetime import datetime

    from canonic.compiler.query import SemanticQuery
    from canonic.contracts.models import FinalityRule
    from canonic.contracts.resolver import ContractResolver
    from canonic.semantic.models import Dimension, SemanticSource


_FANOUT = frozenset({Relationship.ONE_TO_MANY, Relationship.MANY_TO_MANY})


_SQLITE_DATE_MOD_RE = re.compile(r"^([+-]?)\s*(\d+)\s+(\w+)$")


_SQLITE_DATE_TRUNC_RE = re.compile(r"^start of (day|week|month|year)$", re.IGNORECASE)


def _apply_sqlite_date_modifier(base: exp.Expression, modifier: str) -> exp.Expression | None:
    """Apply a single SQLite date() modifier to `base`; return None if unsupported."""
    trunc = _SQLITE_DATE_TRUNC_RE.match(modifier.strip())
    if trunc:
        return _func("DATE_TRUNC", exp.Literal.string(trunc.group(1).lower()), base)
    m = _SQLITE_DATE_MOD_RE.match(modifier.strip())
    if m:
        sign, num, unit = m.groups()
        interval = exp.Interval(this=exp.Literal.string(num), unit=exp.Var(this=unit.upper()))
        return (
            exp.Sub(this=base, expression=interval)
            if sign == "-"
            else exp.Add(this=base, expression=interval)
        )
    return None


def _rewrite_sqlite_date_modifiers(node: exp.Expression) -> exp.Expression:
    """Rewrite SQLite DATE('now', modifier, ...) → CURRENT_DATE with equivalent arithmetic.

    SQLite applies modifiers left-to-right, e.g. DATE('now', 'start of month', '-1 month')
    truncates to the current month, then subtracts one month.
    """
    if not isinstance(node, exp.Date):
        return node
    if not isinstance(node.this, exp.Literal) or node.this.name != "now":
        return node
    zone = node.args.get("zone")
    if zone is None:
        return node
    modifiers = [zone.name, *(e.name for e in node.args.get("expressions") or [])]
    result: exp.Expression = exp.CurrentDate()
    for modifier in modifiers:
        applied = _apply_sqlite_date_modifier(result, modifier)
        if applied is None:
            return node
        result = applied
    return result


# sqlglot builds many node classes dynamically, so mypy cannot see their inheritance
# from ``exp.Expression``. These thin wrappers cast at the boundary so the rest of the
# module stays strictly typed.
def _parse(sql: str) -> exp.Expression:
    parsed = cast("exp.Expression", sqlglot.parse_one(sql))
    return parsed.transform(_rewrite_sqlite_date_modifiers)


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


def _dimension_output_names(dimensions: list[tuple[str, Dimension]]) -> list[str]:
    """Output column name for each requested dimension.

    Different join roles can resolve to dimensions sharing the same bare name (e.g.
    ``pickup.city`` and ``dropoff.city`` both name a ``city`` dimension on the same
    joined source). Aliasing both to bare ``city`` in the emitted SQL collapses them
    into a single column and corrupts the GROUP BY. Qualify with the role alias only
    when the bare name isn't unique within this dimension list.
    """
    counts: dict[str, int] = {}
    for _role, dim in dimensions:
        counts[dim.name] = counts.get(dim.name, 0) + 1
    return [dim.name if counts[dim.name] == 1 else f"{role}.{dim.name}" for role, dim in dimensions]


def _resolve_dimensions(
    query: SemanticQuery,
    sources_by_name: dict[str, SemanticSource],
    owner: str,
    alias_to_source: dict[str, str],
) -> list[tuple[str, Dimension]]:
    resolved: list[tuple[str, Dimension]] = []
    for name in query.dimensions:
        found = _find_dimension(name, sources_by_name, owner, alias_to_source)
        if found is None:
            reachable_sources = {
                src_name: sources_by_name[src_name]
                for src_name in set(alias_to_source.values())
                if src_name in sources_by_name
            }
            suggestions = _dimension_suggestions(name, reachable_sources)
            hint = f"; did you mean: {', '.join(suggestions)}" if suggestions else ""
            raise UnreachableError(
                f"dimension {name!r} is not declared on any reachable source{hint}",
                candidates=suggestions,
            )
        resolved.append(found)
    return resolved


def _bind_filters(
    filters: list[str],
    sources_by_name: dict[str, SemanticSource],
    owner: str,
    alias_to_source: dict[str, str] | None = None,
) -> tuple[list[exp.Expression], set[str]]:
    """Parse filter strings, qualify referenced names to their owning source alias."""
    conditions: list[exp.Expression] = []
    used: set[str] = set()
    for raw in filters:
        parsed = _parse(raw)
        bound, sources = _qualify_columns(parsed, sources_by_name, owner, alias_to_source)
        conditions.append(bound)
        used |= sources
    return conditions, used


def _population_filter_conditions(
    population_filter: str | None,
    sources_by_name: dict[str, SemanticSource],
    owner: str,
    alias_to_source: dict[str, str] | None = None,
) -> list[exp.Expression]:
    """Parse population_filter and qualify its columns to ``owner`` (§4.5). Empty if None."""
    if not population_filter:
        return []
    parsed = _parse(population_filter)
    bound, _ = _qualify_columns(parsed, sources_by_name, owner, alias_to_source)
    return [bound]


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


def _guard_aggregate(agg: exp.Expression, condition: exp.Expression) -> exp.Expression:
    """Scope one metric's own aggregate to rows matching ``condition`` via conditional
    aggregation (``SUM(CASE WHEN condition THEN x END)``), rather than ANDing the
    condition into a WHERE shared with sibling metrics in the same flat SELECT — which
    would drop rows those sibling metrics still need (GH multi-metric population_filter
    collision).
    """
    inner = agg.this
    if isinstance(inner, exp.Distinct):
        guarded = [exp.Case(ifs=[exp.If(this=condition.copy(), true=e)]) for e in inner.expressions]
        agg.set("this", exp.Distinct(expressions=guarded))
    elif isinstance(inner, exp.Star):
        agg.set("this", exp.Case(ifs=[exp.If(this=condition, true=exp.Literal.number(1))]))
    else:
        agg.set("this", exp.Case(ifs=[exp.If(this=condition, true=inner)]))
    return agg


def _requalify_all(expr: exp.Expression, new_alias: str) -> exp.Expression:
    """Replace every qualified column's table with ``new_alias``, regardless of its
    current table. Used to re-point a per-metric guard condition at a dedup subquery's
    bare-named inner projection (``_DEDUP_ALIAS``), where the original source alias no
    longer applies.
    """

    def _transform(node: exp.Expression) -> exp.Expression:
        if isinstance(node, exp.Column) and node.table:
            return exp.column(node.name, table=new_alias)
        return node

    return expr.transform(_transform)


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
        on_ast = _parse(edge.on_sql)
        select = select.join(
            _alias(exp.to_table(target.table), edge.alias),
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
    metric_conditions: list[list[exp.Expression]] | None = None,
) -> exp.Select:
    """Single-SELECT emission for the no-fanout case (SPEC §4 step 7).

    ``metric_conditions``, when given, holds one condition list per metric (its own
    population_filter/guardrails) applied via conditional aggregation on that metric's
    own projection — used when multiple metrics share this SELECT and a shared WHERE
    would incorrectly apply one metric's restriction to every other metric's aggregate.
    """
    select = exp.Select()
    projections: list[exp.Expression] = []
    group_exprs: list[exp.Expression] = []
    for (src, dim), name in zip(dimensions, _dimension_output_names(dimensions), strict=True):
        expr = _dimension_expr(src, dim)
        projections.append(_alias(expr, name))
        group_exprs.append(expr)
    for i, m in enumerate(metrics):
        expr = _measure_expr(m.source, m.measure)
        conditions = metric_conditions[i] if metric_conditions else []
        if conditions:
            expr = _guard_aggregate(expr, cast("exp.Expression", exp.and_(*conditions)))
        projections.append(_alias(expr, m.measure.name))
    select = select.select(*projections)
    select = _from_and_joins(select, owner, join_edges, sources_by_name)
    if where_conditions:
        select = select.where(exp.and_(*where_conditions))
    if group_exprs:
        select = select.group_by(*group_exprs)
    return select


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


def _dim_matches(dim: Dimension, name: str) -> bool:
    """True when *name* matches a dimension by canonical name or any declared alias."""
    return dim.name == name or name in dim.aliases


def _dimension_suggestions(name: str, sources_by_name: dict[str, SemanticSource]) -> list[str]:
    """Fuzzy-match *name* against all dimension names, labels, and aliases across sources."""
    import difflib

    token_to_canonical: dict[str, str] = {}
    for src in sources_by_name.values():
        for dim in src.dimensions:
            token_to_canonical[dim.name.lower()] = dim.name
            for alias in dim.aliases:
                token_to_canonical[alias.lower()] = dim.name
            if dim.label:
                token_to_canonical[dim.label.lower()] = dim.name
    matches = difflib.get_close_matches(name.lower(), token_to_canonical, n=5, cutoff=0.5)
    seen: set[str] = set()
    result: list[str] = []
    for m in matches:
        canonical = token_to_canonical[m]
        if canonical not in seen:
            seen.add(canonical)
            result.append(canonical)
    return result


def _find_dimension(
    name: str,
    sources_by_name: dict[str, SemanticSource],
    owner: str | None = None,
    alias_to_source: dict[str, str] | None = None,
) -> tuple[str, Dimension] | None:
    # Handle qualified references like "pickup.city".
    if "." in name:
        role, dim_name = name.split(".", 1)
        if alias_to_source is None or role not in alias_to_source:
            return None
        src_name = alias_to_source[role]
        source = sources_by_name.get(src_name)
        if source is None:
            return None
        dim = next((d for d in source.dimensions if _dim_matches(d, dim_name)), None)
        if dim is None:
            return None
        return (role, dim)

    if owner is not None:
        # Priority 1: the metric's owning source wins unconditionally.
        owner_source = sources_by_name.get(owner)
        if owner_source is not None:
            for dim in owner_source.dimensions:
                if _dim_matches(dim, name):
                    return owner, dim

        # Priority 2: search join-reachable aliases for the dimension.
        if alias_to_source is not None:
            reachable: dict[str, str] = {
                alias: src for alias, src in alias_to_source.items() if alias != owner
            }
        else:
            reachable = {src: src for src in _reachable_from(owner, sources_by_name)}

        candidates: list[tuple[str, Dimension]] = []
        for alias in sorted(reachable):
            reachable_src_name = reachable[alias]
            reachable_src = sources_by_name.get(reachable_src_name)
            if reachable_src is None:
                continue
            for dim in reachable_src.dimensions:
                if _dim_matches(dim, name):
                    candidates.append((alias, dim))
        if len(candidates) == 1:
            return candidates[0]
        if len(candidates) > 1:
            qualified = [f"{alias}.{dim.name}" for alias, dim in candidates]
            raise Ambiguous(
                f"dimension {name!r} is present on multiple join-reachable sources; "
                f"qualify explicitly",
                candidates=qualified,
            )
        return None

    # No owner: fall back to alphabetical scan (preserves behaviour for callers without context).
    for src in sorted(sources_by_name):
        for dim in sources_by_name[src].dimensions:
            if _dim_matches(dim, name):
                return src, dim
    return None


def _bind_name(
    name: str,
    sources_by_name: dict[str, SemanticSource],
    owner: str | None = None,
    alias_to_source: dict[str, str] | None = None,
) -> tuple[str, str] | None:
    """Resolve a bare name to ``(alias, physical_column)`` — dimension first, then column.

    Prioritizes the owner source when resolving columns to avoid ambiguity;
    a distinct_on column should resolve to the metric's owning source if present.
    """
    dim = _find_dimension(name, sources_by_name, owner, alias_to_source)
    if dim is not None:
        return dim[0], dim[1].column

    # Priority 1: check the owner source first (if provided).
    if owner is not None:
        owner_source = sources_by_name.get(owner)
        if owner_source is not None and any(c.name == name for c in owner_source.columns):
            return owner, name

    # Priority 2: check other sources (sorted for determinism).
    for src in sorted(sources_by_name):
        if src == owner:
            continue  # already checked above
        if any(c.name == name for c in sources_by_name[src].columns):
            return src, name
    return None


def _qualify_columns(
    expr: exp.Expression,
    sources_by_name: dict[str, SemanticSource],
    owner: str | None = None,
    alias_to_source: dict[str, str] | None = None,
) -> tuple[exp.Expression, set[str]]:
    """Qualify each bare column in a filter to its owning source alias + physical column."""
    used: set[str] = set()

    def transform(node: exp.Expression) -> exp.Expression:
        if isinstance(node, exp.Column):
            if node.table:
                used.add(node.table)
                return node
            binding = _bind_name(node.name, sources_by_name, owner, alias_to_source)
            if binding is None:
                raise UnreachableError(f"filter references unknown name {node.name!r}")
            src_or_alias, col = binding
            used.add(src_or_alias)
            return exp.column(col, table=src_or_alias)
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


_TIME_TYPES = frozenset({NormalizedType.DATE, NormalizedType.TIMESTAMP})


def _find_time_dim_name(
    dimensions: list[tuple[str, Dimension]],
    sources_by_name: dict[str, SemanticSource],
    alias_to_source: dict[str, str] | None = None,
) -> str | None:
    """Return the name of the first DATE/TIMESTAMP dimension in the query, or None."""
    for alias_or_src, dim in dimensions:
        src_name = (alias_to_source or {}).get(alias_or_src, alias_or_src)
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
    measure_alias: str | None = None,
) -> exp.Expression:
    """Build a UNION ALL over finality realizations (SPEC-E5-E15 stage 5).

    Each branch selects from one realization source, gated by the watermark on the time
    dimension column, and projects an ``is_final`` boolean marker.  All WHERE conditions
    from the original owner (user filters, guardrails, population filters) are re-qualified
    to each branch source — this is valid because both sources share the same column/
    dimension schema.

    ``measure_alias`` overrides the column alias for each metric's measure projection; used
    when building a leaf sub-query for a composite metric (e.g. alias ``"n"``/``"d"``).
    """
    from canonic.contracts.finality import watermark_to_iso

    # Any table alias referenced by the incoming WHERE conditions (filters, guardrails,
    # population filters) must also be joined in each branch — they get re-qualified onto
    # the branch below but not re-planned, so their source tables need to already be there.
    where_source_aliases = {
        col.table for cond in where_conditions for col in cond.find_all(exp.Column) if col.table
    }

    watermark_iso = watermark_to_iso(watermark_dt)
    watermark_lit = cast(
        "exp.Expression",
        exp.Cast(
            this=exp.Literal.string(watermark_iso),
            to=exp.DataType.build("TIMESTAMPTZ"),
        ),
    )

    dim_names = _dimension_output_names(dimensions)

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

        # Resolve dimensions with join-awareness (dimensions may live on joined sources).
        branch_alias_to_source = build_alias_tree(src_name, sources_by_name)
        branch_dims: list[tuple[str, Dimension]] = []
        for _orig, dim in dimensions:
            resolved = _find_dimension(
                dim.name, sources_by_name, owner=src_name, alias_to_source=branch_alias_to_source
            )
            if resolved is None:
                raise UnreachableError(
                    f"finality realization source {src_name!r} does not declare "
                    f"dimension {dim.name!r}"
                )
            branch_dims.append(resolved)

        # Plan joins needed for non-owner dimensions, plus non-owner filter/guardrail
        # sources (WHERE conditions are re-qualified onto this branch below, so any
        # table they reference must be joined here too).
        needed_targets = {
            branch_alias_to_source[alias]
            for alias, _ in branch_dims
            if alias != src_name and alias in branch_alias_to_source
        }
        needed_targets |= {
            branch_alias_to_source[alias]
            for alias in where_source_aliases
            if alias != src_name and alias in branch_alias_to_source
        }
        branch_join_edges: list[JoinEdge] = (
            plan_joins(src_name, needed_targets, sources_by_name) if needed_targets else []
        )

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
        for (b_src, dim), name in zip(branch_dims, dim_names, strict=True):
            expr = _dimension_expr(b_src, dim)
            projections.append(_alias(expr, name))
            group_exprs.append(expr)
        for m in branch_metrics:
            col_alias = measure_alias if measure_alias is not None else m.measure.name
            projections.append(_alias(_measure_expr(m.source, m.measure), col_alias))
        projections.append(_alias(is_final_val, "is_final"))
        select = select.select(*projections)
        select = _from_and_joins(select, src_name, branch_join_edges, sources_by_name)
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
