"""Simple/additive compile path (stages 2-8, the default route, SPEC §4)."""

from __future__ import annotations

import logging
from datetime import datetime
from typing import TYPE_CHECKING, cast

from sqlglot import exp

from canonic.compiler.dialect import adapter_for
from canonic.compiler.joins import JoinEdge, build_alias_tree, plan_joins
from canonic.compiler.result import (
    CompileResult,
    FinalityMetadata,
)
from canonic.exc import FanoutUnsafe, GuardrailBlock, Unresolved, UnsupportedMeasure
from canonic.semantic.models import Additivity

if TYPE_CHECKING:
    from canonic.compiler.query import SemanticQuery
    from canonic.contracts.models import FinalityRule
    from canonic.contracts.resolver import Binding as ResolverBinding
    from canonic.contracts.resolver import ContractResolver
    from canonic.semantic.models import Dimension, SemanticSource

from canonic.compiler._helpers import (
    _FANOUT,
    _TIME_TYPES,
    _alias,
    _bind_filters,
    _build_finality_union,
    _build_simple,
    _dimension_expr,
    _dimension_output_names,
    _enforce_guardrails,
    _find_measure,
    _find_time_dim_name,
    _freshness,
    _from_and_joins,
    _input_columns,
    _parse,
    _population_filter_conditions,
    _qualify_to,
    _resolve_dimensions,
    _ResolvedMetric,
)

logger = logging.getLogger(__name__)

_DEDUP_ALIAS = "_base"


def _bindings_to_resolved(
    name_bindings: list[tuple[str, ResolverBinding]],
    sources_by_name: dict[str, SemanticSource],
) -> list[_ResolvedMetric]:
    """Convert pre-resolved single-kind bindings to _ResolvedMetric objects."""
    resolved: list[_ResolvedMetric] = []
    for name, binding in name_bindings:
        assert binding.source is not None and binding.measure is not None  # noqa: S101
        source = sources_by_name.get(binding.source)
        if source is None:
            raise Unresolved(f"metric {name!r} binds to unknown source {binding.source!r}")
        measure = _find_measure(source, binding.measure)
        if measure is None:
            raise Unresolved(
                f"metric {name!r} binds to unknown measure {binding.source}.{binding.measure!r}"
            )
        resolved.append(_ResolvedMetric(name=name, source=binding.source, measure=measure))
    return resolved


def _compile_simple_additive(
    query: SemanticQuery,
    raw_bindings: list[tuple[str, ResolverBinding]],
    resolver: ContractResolver,
    sources_by_name: dict[str, SemanticSource],
    *,
    dialect: str = "postgres",
) -> CompileResult:
    """Compile the simple/additive path (stages 2–8) — no composite, semi-additive, or recompute."""
    metrics = _bindings_to_resolved(raw_bindings, sources_by_name)
    owner = metrics[0].source  # FROM anchor

    # Stage 2 — resolve dimensions & filters to owning aliases.
    logger.debug("stage 2: resolving dimensions and filters")
    alias_to_source = build_alias_tree(owner, sources_by_name)
    dimensions = _resolve_dimensions(query, sources_by_name, owner, alias_to_source)
    referenced = {alias for alias, _ in dimensions}
    where_conditions, filter_sources = _bind_filters(
        query.filters, sources_by_name, owner, alias_to_source
    )
    referenced |= filter_sources
    referenced |= {owner}

    # Stage 3 — plan the join graph from the owner to every referenced alias.
    logger.debug("stage 3: planning join graph")
    join_edges = plan_joins(
        owner, referenced - {owner}, sources_by_name, via=list(query.via) or None
    )

    # Stage 4 — fanout analysis: safety floor (SPEC-E15 §5) + dedup for additive fanout.
    logger.debug("stage 4: fanout analysis")
    fanout = any(edge.join.relationship in _FANOUT for edge in join_edges)
    grouped = {dim.name for _alias, dim in dimensions}

    for m in metrics:
        add = m.measure.additivity
        if add is Additivity.ADDITIVE:
            if not m.measure.is_p0_compilable:
                raise UnsupportedMeasure(
                    f"measure {m.source}.{m.measure.name!r} uses an aggregate function "
                    f"not supported at P0"
                )
            continue

        # Non-additive / semi-additive: safety floor — refuse corrupting aggregations.
        if fanout:
            logger.warning(
                "fanout unsafe: measure %s.%s is %s and a fanout join would corrupt the aggregate",
                m.source,
                m.measure.name,
                add.value,
            )
            raise FanoutUnsafe(
                f"measure {m.source}.{m.measure.name!r} is {add.value} and a "
                f"one_to_many/many_to_many join in this query would multiply its rows "
                f"and corrupt the aggregate; request it without the fanning dimension "
                f"or source, or query it at its native grain"
            )
        if add is Additivity.SEMI_ADDITIVE:
            unsafe_dims = [d for d in m.measure.semi_additive_over if d not in grouped]
            if unsafe_dims:
                raise UnsupportedMeasure(
                    f"measure {m.source}.{m.measure.name!r} is semi-additive over "
                    f"{unsafe_dims} and cannot be collapsed across those dimensions "
                    f"without the semi_additive strategy; group by {unsafe_dims} for "
                    f"a correct result"
                )
        # Pure NON_ADDITIVE with no fanout, or SEMI_ADDITIVE grouped by its collapse dim(s):
        # _build_simple recomputes the aggregate from base rows at the requested grain — safe.

    # Stage 5 — finality & coalescing [P1]: evaluate watermark, select sources per window.
    logger.debug("stage 5: finality evaluation")
    finality_rule = resolver.finality_for(metrics[0].name) if len(metrics) == 1 else None
    time_dim_name: str | None = None
    if finality_rule is not None:
        time_dim_name = _find_time_dim_name(dimensions, sources_by_name, alias_to_source)
        if time_dim_name is None:
            finality_rule = None  # no time dimension → all rows implicitly final

    # Stage 5b — restrict_source: block queries that would pull provisional rows in guarded contexts.
    logger.debug("stage 5b: restrict_source enforcement")
    _enforce_restrict_source(query, metrics, resolver, finality_rule, sources_by_name)

    # population_filter — defines the population the metric is about (§4.5); before guardrails.
    for _, b in raw_bindings:
        where_conditions += _population_filter_conditions(
            b.binding.canonical.population_filter, sources_by_name, owner, alias_to_source
        )

    # Stage 6 — enforce guardrails: AND mandatory filters into WHERE.
    logger.debug("stage 6: enforcing guardrails")
    guard_conditions, fired = _enforce_guardrails(metrics, resolver, query.context, sources_by_name)
    if fired:
        logger.info("guardrails applied: count=%d ids=%s", len(fired), [g.id for g in fired])
    where_conditions += guard_conditions

    # Stage 7 — emit SQL through the dialect adapter.
    logger.debug("stage 7: emitting SQL via dialect %s", dialect)
    finality_meta: FinalityMetadata | None = None
    adapter = adapter_for(dialect)
    if finality_rule is not None and time_dim_name is not None:
        from canonic.contracts.finality import evaluate_watermark, watermark_to_iso

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
    logger.debug("stage 8: attaching result metadata")
    if finality_meta is not None:
        used_sources = sorted(finality_meta.sources_used)
    else:
        # Map aliases back to source names (deduplicated) for freshness metadata.
        used_source_names: set[str] = {owner}
        for e in join_edges:
            used_source_names.add(e.join.to)
        used_sources = sorted(used_source_names)
    return CompileResult(
        sql=sql,
        dialect=adapter.dialect,
        resolved={m.name: f"{m.source}.{m.measure.name}" for m in metrics},
        guardrails_fired=fired,
        freshness=[_freshness(sources_by_name[s]) for s in used_sources],
        warnings=[],
        finality=finality_meta,
    )


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
    dim_names = _dimension_output_names(dimensions)
    inner = exp.Select()
    inner_projections: list[exp.Expression] = []
    measure_inputs: dict[str, exp.Expression] = {}
    for (src, dim), name in zip(dimensions, dim_names, strict=True):
        inner_projections.append(_alias(_dimension_expr(src, dim), name))
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
    for name in dim_names:
        dim_col = exp.column(name, table=_DEDUP_ALIAS)
        projections.append(_alias(dim_col, name))
        group_exprs.append(dim_col)
    for m in metrics:
        projections.append(
            _alias(_qualify_to(_parse(m.measure.expr), _DEDUP_ALIAS), m.measure.name)
        )
    outer = outer.select(*projections).from_(_alias(inner.subquery(), _DEDUP_ALIAS))
    if group_exprs:
        outer = outer.group_by(*group_exprs)
    return outer


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

            from canonic.contracts.finality import evaluate_watermark

            watermark_dt = evaluate_watermark(final_r.watermark, final_r.tz, query.as_of)
            time_names = _time_column_names(rule, sources_by_name)
            if _window_exceeds_watermark(query.filters, time_names, watermark_dt, sources_by_name):
                logger.warning(
                    "restrict_source enforced: guardrail=%s watermark exceeded", guardrail.id
                )
                raise GuardrailBlock(guardrail.rationale)
