"""The deterministic compiler pipeline (SPEC-E5-E15 §4, stages 1–4, 6–8).

``compile`` turns a :class:`SemanticQuery` into dialect-correct, read-only SQL plus
result metadata. No LLM, no wall-clock, no randomness: identical inputs yield
byte-identical SQL (SPEC §8). The :class:`ContractResolver` is the only authority on
canonicality — the compiler trusts its results and never reimplements them (§6).
"""

from __future__ import annotations

import dataclasses
import logging
import re
from datetime import datetime
from typing import TYPE_CHECKING, cast

import sqlglot
from sqlglot import exp

from canonic.compiler.dialect import (  # noqa: F401 — re-exported
    DIALECT_ADAPTERS,
    DialectAdapter,
    adapter_for,
)
from canonic.compiler.joins import JoinEdge, build_alias_tree, plan_joins, reachable_dimension_names
from canonic.compiler.result import (
    CompileResult,
    CompositionMetadata,
    FinalityMetadata,
    FiredGuardrail,
    OpaqueMetadata,
    PartialAdditiveMetadata,
    RecomputeAtGrainMetadata,
    RelatedDimension,
    RelatedMetadata,
    RelatedMetric,
    SourceFreshness,
    TrustInput,
)
from canonic.contracts.models import BindingKind, CollapseAgg, OnZeroDenominator
from canonic.contracts.resolver import Ambiguous as ResolverAmbiguous
from canonic.contracts.resolver import Binding as ResolverBinding
from canonic.contracts.resolver import ComponentBindings, RecomputeAtGrainBinding
from canonic.contracts.resolver import Unresolved as ResolverUnresolved
from canonic.exc import Ambiguous, FanoutUnsafe, GuardrailBlock, Unresolved, UnsupportedMeasure
from canonic.exc import Unreachable as UnreachableError
from canonic.semantic.models import Additivity, Measure, NormalizedType, Relationship
from canonic.trust.models import TrustTier, tier_meets
from canonic.trust.scorer import TrustScorer
from canonic.trust.signals import static_signals_for

if TYPE_CHECKING:
    from collections.abc import Mapping

    from canonic.compiler.query import SemanticQuery
    from canonic.contracts.models import FinalityRule
    from canonic.contracts.resolver import ContractResolver
    from canonic.semantic.models import Dimension, SemanticSource

logger = logging.getLogger(__name__)

__all__ = ["compile"]

_DEDUP_ALIAS = "_base"
_FANOUT = frozenset({Relationship.ONE_TO_MANY, Relationship.MANY_TO_MANY})


_SQLITE_DATE_MOD_RE = re.compile(r"^([+-]?)\s*(\d+)\s+(\w+)$")


def _rewrite_sqlite_date_modifiers(node: exp.Expression) -> exp.Expression:
    """Rewrite SQLite DATE('now', modifier) → CURRENT_DATE ± INTERVAL 'N unit'."""
    if not isinstance(node, exp.Date):
        return node
    zone = node.args.get("zone")
    if zone is None:
        return node
    if not isinstance(node.this, exp.Literal) or node.this.name != "now":
        return node
    m = _SQLITE_DATE_MOD_RE.match(zone.name.strip())
    if not m:
        return node
    sign, num, unit = m.groups()
    interval = exp.Interval(this=exp.Literal.string(num), unit=exp.Var(this=unit.upper()))
    if sign == "-":
        return exp.Sub(this=exp.CurrentDate(), expression=interval)
    return exp.Add(this=exp.CurrentDate(), expression=interval)


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


def _dialect_for_bindings(
    raw_bindings: list[tuple[str, ResolverBinding]],
    sources_by_name: dict[str, SemanticSource],
    connection_dialects: Mapping[str, str] | None,
) -> str:
    """Return the sqlglot dialect name for the primary binding's owning connection."""
    if not connection_dialects:
        return "postgres"
    for _, b in raw_bindings:
        source_name = b.source
        if source_name is None and b.components is not None:
            source_name = b.components.numerator.source
        if source_name is None:
            continue
        src = sources_by_name.get(source_name)
        if src is not None and src.connection:
            return connection_dialects.get(src.connection, "postgres")
    return "postgres"


def _trust_inputs_for(
    raw_bindings: list[tuple[str, ResolverBinding]],
    resolver: ContractResolver,
) -> list[TrustInput]:
    """Gather static per-metric trust signals once, shared by every compile path (SPEC-E14 §4)."""
    inputs: list[TrustInput] = []
    for name, binding in raw_bindings:
        has_assertion = bool(resolver.assertions_for({"metrics": [name]}))
        inputs.append(
            TrustInput(
                metric=name,
                provenance=binding.binding.provenance.value,
                has_assertion=has_assertion,
            )
        )
    return inputs


def _enforce_min_trust(
    raw_bindings: list[tuple[str, ResolverBinding]],
    resolver: ContractResolver,
    context: str | None,
    trust_inputs: list[TrustInput],
) -> None:
    """Stage 6b: raise GuardrailBlock when a min_trust guardrail's floor is not met (SPEC-E14 §7).

    Enforced from the static signal set only (provenance, assertion coverage) — the signals
    known before SQL is generated. Only metrics with a single resolved (source, measure) are
    matched (SINGLE/SEMI_ADDITIVE/OPAQUE kinds); composite (ratio/weighted_avg) and
    recompute_at_grain metrics have no single source/measure pair to match against
    ``applies_to``, the same limitation ``restrict_source`` already has.
    """
    if context is None:
        return
    score = TrustScorer.score(static_signals_for(trust_inputs))
    for _name, binding in raw_bindings:
        if binding.source is None or binding.measure is None:
            continue
        for guardrail in resolver.min_trust_for(binding.source, binding.measure, context):
            assert guardrail.level is not None  # noqa: S101 — enforced by model_validator
            floor = TrustTier(guardrail.level)
            if not tier_meets(score.tier, floor):
                logger.warning(
                    "min_trust enforced: guardrail=%s tier=%s required=%s",
                    guardrail.id,
                    score.tier.value,
                    floor.value,
                )
                raise GuardrailBlock(guardrail.rationale)


class _ResolvedMetric:
    """A metric request bound to its canonical source and measure (stage 1)."""

    __slots__ = ("measure", "name", "source")

    def __init__(self, name: str, source: str, measure: Measure) -> None:
        self.name = name
        self.source = source
        self.measure = measure


class _LeafPlan:
    """A compiled leaf query for one component of a composable_post_agg metric (§4.1, §6)."""

    __slots__ = ("dim_names", "finality", "fired", "select", "used_sources", "warnings")

    def __init__(
        self,
        select: exp.Expression,
        fired: list[FiredGuardrail],
        used_sources: set[str],
        warnings: list[str],
        dim_names: list[str],
        finality: FinalityMetadata | None = None,
    ) -> None:
        self.select = select
        self.fired = fired
        self.used_sources = used_sources
        self.warnings = warnings
        self.dim_names = dim_names
        self.finality = finality


def compile(  # noqa: A001 — the public verb for this capability is "compile"
    query: SemanticQuery,
    resolver: ContractResolver,
    sources: list[SemanticSource],
    *,
    connection_dialects: Mapping[str, str] | None = None,
) -> CompileResult:
    """Compile a semantic query to read-only SQL and result metadata (SPEC §4)."""
    sources_by_name = {s.name: s for s in sources}

    # Stage 1 — resolve metric bindings; detect composite kinds and route accordingly.
    logger.debug("stage 1: resolving metric bindings for metrics=%s", query.metrics)
    if not query.metrics:
        raise Unresolved("query requests at least one metric")
    raw_bindings: list[tuple[str, ResolverBinding]] = []
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
        raw_bindings.append((name, result))

    # Compute related metadata once here using resolved bindings (all paths get it via
    # dataclasses.replace or direct constructor argument below).
    queried_sources: set[str] = set()
    for _, b in raw_bindings:
        if b.source is not None:
            queried_sources.add(b.source)
        elif b.components is not None:
            for component in (b.components.numerator, b.components.denominator):
                if component.source is not None:
                    queried_sources.add(component.source)
    queried_metric_names = {name for name, _ in raw_bindings}
    related = _related(queried_sources, queried_metric_names, query, resolver, sources_by_name)
    trust_inputs = _trust_inputs_for(raw_bindings, resolver)
    _enforce_min_trust(raw_bindings, resolver, query.context, trust_inputs)

    composite_indices = [
        i
        for i, (_, b) in enumerate(raw_bindings)
        if b.kind in {BindingKind.RATIO, BindingKind.WEIGHTED_AVG}
    ]
    semi_additive_indices = [
        i for i, (_, b) in enumerate(raw_bindings) if b.kind is BindingKind.SEMI_ADDITIVE
    ]
    recompute_indices = [
        i
        for i, (_, b) in enumerate(raw_bindings)
        if b.kind in {BindingKind.DISTINCT_COUNT, BindingKind.PERCENTILE}
    ]
    opaque_indices = [i for i, (_, b) in enumerate(raw_bindings) if b.kind is BindingKind.OPAQUE]

    # Derive the target SQL dialect from the primary binding's connection.
    dialect = _dialect_for_bindings(raw_bindings, sources_by_name, connection_dialects)

    if composite_indices:
        if len(query.metrics) > 1:
            raise UnsupportedMeasure(
                "composite metrics (ratio/weighted_avg) must be queried alone; "
                "remove other metrics from the request or split into separate queries"
            )
        _, composite = raw_bindings[0]
        logger.info("compile path: composite metric=%s", query.metrics[0])
        return dataclasses.replace(
            _compile_composite(query, composite, resolver, sources_by_name, dialect=dialect),
            related=related,
            trust_inputs=trust_inputs,
        )
    if semi_additive_indices:
        if len(query.metrics) > 1:
            raise UnsupportedMeasure(
                "semi_additive metrics must be queried alone; "
                "remove other metrics from the request or split into separate queries"
            )
        _, sa_binding = raw_bindings[0]
        logger.info("compile path: semi_additive metric=%s", query.metrics[0])
        return dataclasses.replace(
            _compile_semi_additive(query, sa_binding, resolver, sources_by_name, dialect=dialect),
            related=related,
            trust_inputs=trust_inputs,
        )
    if recompute_indices:
        if len(query.metrics) > 1:
            raise UnsupportedMeasure(
                "recompute_at_grain metrics (distinct_count/percentile) must be queried alone; "
                "remove other metrics from the request or split into separate queries"
            )
        _, rg_binding = raw_bindings[0]
        logger.info("compile path: recompute_at_grain metric=%s", query.metrics[0])
        return dataclasses.replace(
            _compile_recompute_at_grain(
                query, rg_binding, resolver, sources_by_name, dialect=dialect
            ),
            related=related,
            trust_inputs=trust_inputs,
        )
    if opaque_indices:
        if len(query.metrics) > 1:
            raise UnsupportedMeasure(
                "opaque metrics must be queried alone; "
                "remove other metrics from the request or split into separate queries"
            )
        _, opaque_binding = raw_bindings[0]
        logger.info("compile path: opaque metric=%s", query.metrics[0])
        return dataclasses.replace(
            _compile_opaque(query, opaque_binding, resolver, sources_by_name, dialect=dialect),
            related=related,
            trust_inputs=trust_inputs,
        )

    logger.info("compile path: simple/additive metrics=%s", query.metrics)
    return dataclasses.replace(
        _compile_simple_additive(query, raw_bindings, resolver, sources_by_name, dialect=dialect),
        related=related,
        trust_inputs=trust_inputs,
    )


# --- Stage 1 -----------------------------------------------------------------


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


# --- Composite compile path (composable_post_agg, §4.1) ----------------------


def _plan_leaf(
    component: ResolverBinding,
    query: SemanticQuery,
    resolver: ContractResolver,
    sources_by_name: dict[str, SemanticSource],
    measure_alias: str,
    population_filter: str | None = None,
) -> _LeafPlan:
    """Run stages 2–6 for one single-kind component; return a leaf SELECT.

    Each component is planned as an independent sub-query at the requested grain so
    its own guardrails and safety-floor checks fire automatically (SPEC §4.1, §6, AC3).
    When the component metric has a finality rule and the query includes a time dimension,
    the leaf is emitted as a finality UNION ALL (projecting ``is_final``) — so the compose
    step can inherit the most conservative signal across leaves (§7, S6 AC1).
    """
    if component.kind is not BindingKind.SINGLE:
        raise UnsupportedMeasure(
            f"nested composite metrics are not yet supported; "
            f"component {component.metric!r} has kind {component.kind!r}"
        )
    assert component.source is not None and component.measure is not None  # noqa: S101

    source_name = component.source
    alias_to_source = build_alias_tree(source_name, sources_by_name)

    # Stage 2 — dimensions & filters relative to this component's source.
    dimensions = _resolve_dimensions(query, sources_by_name, source_name, alias_to_source)
    referenced = {alias for alias, _ in dimensions}
    where_conditions, filter_sources = _bind_filters(
        query.filters, sources_by_name, source_name, alias_to_source
    )
    referenced |= filter_sources
    referenced |= {source_name}

    # Stage 3 — join graph from this component's owner.
    join_edges = plan_joins(
        source_name, referenced - {source_name}, sources_by_name, via=list(query.via) or None
    )

    # Stage 4 — safety floor for this component.
    fanout = any(edge.join.relationship in _FANOUT for edge in join_edges)
    grouped = {dim.name for _alias, dim in dimensions}

    source_obj = sources_by_name.get(source_name)
    if source_obj is None:
        raise Unresolved(f"component {component.metric!r} binds to unknown source {source_name!r}")
    measure_obj = _find_measure(source_obj, component.measure)
    if measure_obj is None:
        raise Unresolved(
            f"component {component.metric!r} binds to unknown measure "
            f"{source_name}.{component.measure!r}"
        )

    add = measure_obj.additivity
    if add is Additivity.ADDITIVE:
        if not measure_obj.is_p0_compilable:
            raise UnsupportedMeasure(
                f"measure {source_name}.{measure_obj.name!r} uses an aggregate function "
                f"not supported at P0"
            )
    elif fanout:
        raise FanoutUnsafe(
            f"measure {source_name}.{measure_obj.name!r} is {add.value} and a "
            f"one_to_many/many_to_many join in this query would multiply its rows "
            f"and corrupt the aggregate; request it without the fanning dimension "
            f"or source, or query it at its native grain"
        )
    elif add is Additivity.SEMI_ADDITIVE:
        unsafe_dims = [d for d in measure_obj.semi_additive_over if d not in grouped]
        if unsafe_dims:
            raise UnsupportedMeasure(
                f"measure {source_name}.{measure_obj.name!r} is semi-additive over "
                f"{unsafe_dims} and cannot be collapsed across those dimensions "
                f"without the semi_additive strategy; group by {unsafe_dims} for "
                f"a correct result"
            )

    # population_filter — defines the population this leaf is compiled over (§4.5); before guardrails.
    where_conditions += _population_filter_conditions(
        population_filter, sources_by_name, source_name, alias_to_source
    )

    # Stage 6 — guardrails for this leaf.
    guard_conditions, fired = _enforce_guardrails(
        [_ResolvedMetric(name=component.metric, source=source_name, measure=measure_obj)],
        resolver,
        query.context,
        sources_by_name,
    )
    where_conditions += guard_conditions

    # Stage 5 — finality per leaf (§7, S6): build a UNION ALL when the component metric
    # has a finality rule and the query includes a time dimension, so the compose step
    # can apply the conservative-merge rule across leaves.
    leaf_finality: FinalityMetadata | None = None
    finality_rule = resolver.finality_for(component.metric)
    time_dim_name: str | None = None
    if finality_rule is not None:
        time_dim_name = _find_time_dim_name(dimensions, sources_by_name, alias_to_source)
        if time_dim_name is None:
            finality_rule = None  # no time dimension → all rows implicitly final

    if finality_rule is not None and time_dim_name is not None:
        from canonic.contracts.finality import evaluate_watermark, watermark_to_iso

        final_r = next(r for r in finality_rule.realizations if r.role == "final")
        watermark_dt = evaluate_watermark(
            cast("str", final_r.watermark), cast("str", final_r.tz), query.as_of
        )
        leaf_metrics = [
            _ResolvedMetric(name=component.metric, source=source_name, measure=measure_obj)
        ]
        leaf_select: exp.Expression = _build_finality_union(
            rule=finality_rule,
            query_metrics=leaf_metrics,
            dimensions=dimensions,
            where_conditions=where_conditions,
            sources_by_name=sources_by_name,
            watermark_dt=watermark_dt,
            time_dim_name=time_dim_name,
            original_owner=source_name,
            measure_alias=measure_alias,
        )
        leaf_finality = FinalityMetadata(
            watermark=watermark_to_iso(watermark_dt),
            sources_used=[r.source for r in finality_rule.realizations],
            result_flag=finality_rule.result_flag or "per_row",
        )
        used = {r.source for r in finality_rule.realizations}
    else:
        leaf_select = _build_leaf_select(
            source_name,
            measure_obj,
            measure_alias,
            dimensions,
            where_conditions,
            join_edges,
            sources_by_name,
        )
        used = {source_name} | {e.join.to for e in join_edges}

    return _LeafPlan(
        select=leaf_select,
        fired=fired,
        used_sources=used,
        warnings=[],
        dim_names=_dimension_output_names(dimensions),
        finality=leaf_finality,
    )


def _build_leaf_select(
    owner: str,
    measure: Measure,
    measure_alias: str,
    dimensions: list[tuple[str, Dimension]],
    where_conditions: list[exp.Expression],
    join_edges: list[JoinEdge],
    sources_by_name: dict[str, SemanticSource],
) -> exp.Select:
    """Build a leaf SELECT projecting dimensions + one measure aliased to measure_alias."""
    select = exp.Select()
    projections: list[exp.Expression] = []
    group_exprs: list[exp.Expression] = []
    for (src, dim), name in zip(dimensions, _dimension_output_names(dimensions), strict=True):
        expr = _dimension_expr(src, dim)
        projections.append(_alias(expr, name))
        group_exprs.append(expr)
    projections.append(_alias(_measure_expr(owner, measure), measure_alias))
    select = select.select(*projections)
    select = _from_and_joins(select, owner, join_edges, sources_by_name)
    if where_conditions:
        select = select.where(exp.and_(*where_conditions))
    if group_exprs:
        select = select.group_by(*group_exprs)
    return select


def _compile_composite(
    query: SemanticQuery,
    composite: ResolverBinding,
    resolver: ContractResolver,
    sources_by_name: dict[str, SemanticSource],
    *,
    dialect: str = "postgres",
) -> CompileResult:
    """Compile a composable_post_agg metric via CTE-per-leaf + divide (SPEC §4.1, §6 step 6b).

    The unifying rule — aggregate first, combine last: each component is planned as an
    independent sub-query (stages 2–4, 6) at the requested grain; the compose step
    divides on the shared grain via ``n / NULLIF(d, 0)`` (or variant per on_zero_denominator).
    """
    assert composite.components is not None  # noqa: S101 — routing guarantees composite kind
    components: ComponentBindings = composite.components
    on_zero = components.on_zero_denominator
    adapter = adapter_for(dialect)
    queried_name = query.metrics[0]

    pop_filter = composite.binding.canonical.population_filter
    num_plan = _plan_leaf(components.numerator, query, resolver, sources_by_name, "n", pop_filter)
    den_plan = _plan_leaf(components.denominator, query, resolver, sources_by_name, "d", pop_filter)

    dim_names = num_plan.dim_names

    # Build the division expression per on_zero_denominator policy.
    n_col = cast("exp.Expression", exp.column("n"))
    d_col = cast("exp.Expression", exp.column("d"))
    if on_zero is OnZeroDenominator.NULL:
        nullif_d = exp.func("NULLIF", d_col, exp.Literal.number(0))
        combine = cast("exp.Expression", exp.Div(this=n_col, expression=nullif_d))
        zero_warnings = [
            f"zero denominator for metric {composite.metric!r} yields NULL "
            f"(on_zero_denominator=null)"
        ]
    elif on_zero is OnZeroDenominator.ZERO:
        nullif_d = exp.func("NULLIF", d_col, exp.Literal.number(0))
        raw_div = cast("exp.Expression", exp.Div(this=n_col, expression=nullif_d))
        combine = cast("exp.Expression", exp.func("COALESCE", raw_div, exp.Literal.number(0)))
        zero_warnings = []
    else:  # ERROR — raw division; the engine raises on zero
        combine = cast("exp.Expression", exp.Div(this=n_col, expression=d_col))
        zero_warnings = []

    # Conservative finality merge across leaves (§7, S6 AC1).
    # A composite row is final iff both leaf rows are final; a leaf without a finality rule
    # contributes TRUE (all its rows are implicitly final).
    num_cte: exp.Expression = num_plan.select
    den_cte: exp.Expression = den_plan.select
    composite_finality: FinalityMetadata | None = None

    either_has_finality = num_plan.finality is not None or den_plan.finality is not None
    if either_has_finality:
        # Ensure both CTE selects project is_final (add TRUE for any leaf without a rule).
        if num_plan.finality is None:
            num_cte = cast("exp.Select", num_cte).select(
                _alias(cast("exp.Expression", exp.true()), "is_final")
            )
        if den_plan.finality is None:
            den_cte = cast("exp.Select", den_cte).select(
                _alias(cast("exp.Expression", exp.true()), "is_final")
            )

        # Build conservative FinalityMetadata: earliest watermark + union of sources.
        leaf_finalities = [f for f in [num_plan.finality, den_plan.finality] if f is not None]
        earliest_watermark = min(f.watermark for f in leaf_finalities)
        finality_sources = sorted({s for f in leaf_finalities for s in f.sources_used})
        composite_finality = FinalityMetadata(
            watermark=earliest_watermark,
            sources_used=finality_sources,
            result_flag="per_row",
        )

    # Outer SELECT: dimensions (unqualified — USING merges them) + combined metric.
    outer = exp.Select()
    projections: list[exp.Expression] = []
    for dim_name in dim_names:
        projections.append(_alias(cast("exp.Expression", exp.column(dim_name)), dim_name))
    projections.append(_alias(combine, composite.metric))
    if either_has_finality:
        # is_final = COALESCE(num.is_final, TRUE) AND COALESCE(den.is_final, TRUE)
        num_is_final = cast("exp.Expression", exp.column("is_final", table="num"))
        den_is_final = cast("exp.Expression", exp.column("is_final", table="den"))
        combined_is_final = cast(
            "exp.Expression",
            exp.And(
                this=_func("COALESCE", num_is_final, cast("exp.Expression", exp.true())),
                expression=_func("COALESCE", den_is_final, cast("exp.Expression", exp.true())),
            ),
        )
        projections.append(_alias(combined_is_final, "is_final"))
    outer = outer.select(*projections)

    # Join the two CTEs: FULL JOIN USING (dims) or CROSS JOIN for the scalar case.
    if dim_names:
        outer = outer.from_(exp.to_table("num"))
        # sqlglot stubs use Expr (invariant list) — list[str] is valid at runtime
        outer = outer.join(exp.to_table("den"), using=dim_names, join_type="FULL")  # type: ignore[arg-type]
    else:
        outer = outer.from_(exp.to_table("num"))
        outer = outer.join(exp.to_table("den"), join_type="CROSS")

    # Wrap with CTEs.
    ast = outer.with_("num", as_=num_cte).with_("den", as_=den_cte)

    sql = adapter.emit(ast, limit=query.limit)

    # Stage 8 — assemble result metadata (union guardrails + freshness across leaves).
    fired_ids: set[str] = set()
    fired: list[FiredGuardrail] = []
    for g in num_plan.fired + den_plan.fired:
        if g.id not in fired_ids:
            fired_ids.add(g.id)
            fired.append(g)

    all_sources = num_plan.used_sources | den_plan.used_sources
    freshness = [_freshness(sources_by_name[s]) for s in sorted(all_sources)]

    num_name = components.numerator.metric
    den_name = components.denominator.metric
    resolved_str = (
        f"weighted_avg({num_name}, {den_name})"
        if composite.kind is BindingKind.WEIGHTED_AVG
        else f"ratio({num_name}, {den_name})"
    )

    return CompileResult(
        sql=sql,
        dialect=adapter.dialect,
        resolved={queried_name: resolved_str},
        guardrails_fired=fired,
        freshness=freshness,
        warnings=zero_warnings,
        finality=composite_finality,
        composition=CompositionMetadata(
            kind=composite.kind,
            numerator=num_name,
            denominator=den_name,
            on_zero_denominator=on_zero,
        ),
    )


# --- Semi-additive compile path (partial_additive, §4.2) ---------------------


def _compile_semi_additive(
    query: SemanticQuery,
    binding: ResolverBinding,
    resolver: ContractResolver,
    sources_by_name: dict[str, SemanticSource],
    *,
    dialect: str = "postgres",
) -> CompileResult:
    """Compile a semi_additive metric via window or nested-aggregate collapse (SPEC §4.2).

    The decision hinges on whether the query groups by ``collapse_dimension``:
    - Grouped by it → additive; plain sum via ``_build_simple``.
    - Not grouped (collapsing across it) → ``_build_semi_additive`` applies collapse_agg.

    Finality (stage 5) is deferred for semi_additive in this release (P1 scope).
    """
    assert binding.semi_additive is not None  # noqa: S101 — routing guarantees semi_additive kind
    sa = binding.semi_additive
    adapter = adapter_for(dialect)
    queried_name = query.metrics[0]

    assert binding.source is not None and binding.measure is not None  # noqa: S101
    source_name = binding.source
    alias_to_source = build_alias_tree(source_name, sources_by_name)

    # Stage 2 — dimensions & filters.
    dimensions = _resolve_dimensions(query, sources_by_name, source_name, alias_to_source)
    referenced = {alias for alias, _ in dimensions}
    where_conditions, filter_sources = _bind_filters(
        query.filters, sources_by_name, source_name, alias_to_source
    )
    referenced |= filter_sources
    referenced |= {source_name}

    # Stage 3 — join graph.
    join_edges = plan_joins(
        source_name, referenced - {source_name}, sources_by_name, via=list(query.via) or None
    )

    # Stage 4 — safety floor.
    fanout = any(edge.join.relationship in _FANOUT for edge in join_edges)
    if fanout:
        raise FanoutUnsafe(
            f"semi_additive metric {queried_name!r} cannot be used with a "
            f"one_to_many/many_to_many join; request it without the fanning dimension"
        )

    source_obj = sources_by_name.get(source_name)
    if source_obj is None:
        raise Unresolved(f"metric {queried_name!r} binds to unknown source {source_name!r}")
    measure_obj = _find_measure(source_obj, binding.measure)
    if measure_obj is None:
        raise Unresolved(
            f"metric {queried_name!r} binds to unknown measure {source_name}.{binding.measure!r}"
        )
    if measure_obj.additivity is not Additivity.ADDITIVE:
        raise UnsupportedMeasure(
            f"semi_additive binding {queried_name!r}: base measure "
            f"{source_name}.{measure_obj.name!r} must be additive"
        )
    if not measure_obj.is_p0_compilable:
        raise UnsupportedMeasure(
            f"measure {source_name}.{measure_obj.name!r} uses an aggregate function "
            f"not supported at P0"
        )

    # population_filter — defines the population this metric is compiled over (§4.5); before guardrails.
    where_conditions += _population_filter_conditions(
        binding.binding.canonical.population_filter, sources_by_name, source_name, alias_to_source
    )

    # Stage 6 — guardrails.
    resolved_metric = _ResolvedMetric(name=queried_name, source=source_name, measure=measure_obj)
    guard_conditions, fired = _enforce_guardrails(
        [resolved_metric], resolver, query.context, sources_by_name
    )
    where_conditions += guard_conditions

    # Resolve collapse_dimension to (alias, Dimension).
    collapse_dim_result = _find_dimension(
        sa.collapse_dimension, sources_by_name, source_name, alias_to_source
    )
    if collapse_dim_result is None:
        raise Unresolved(
            f"semi_additive binding {queried_name!r}: collapse_dimension "
            f"{sa.collapse_dimension!r} is not declared on any source"
        )
    collapse_alias, collapse_dim = collapse_dim_result

    # Branch: is collapse_dimension among the grouped dimensions?
    grouped = {dim.name for _alias, dim in dimensions}
    collapsed = sa.collapse_dimension not in grouped

    # Stage 7 — emit SQL.
    ast: exp.Expression
    if not collapsed:
        ast = _build_simple(
            source_name,
            [resolved_metric],
            dimensions,
            where_conditions,
            join_edges,
            sources_by_name,
        )
    else:
        ast = _build_semi_additive(
            owner=source_name,
            measure=measure_obj,
            metric_name=queried_name,
            collapse_alias=collapse_alias,
            collapse_dim=collapse_dim,
            dimensions=dimensions,
            where_conditions=where_conditions,
            join_edges=join_edges,
            sources_by_name=sources_by_name,
            collapse_agg=sa.collapse_agg,
        )

    sql = adapter.emit(ast, limit=query.limit)

    # Stage 8 — result metadata.
    used_sources = sorted({source_name} | {e.join.to for e in join_edges})
    return CompileResult(
        sql=sql,
        dialect=adapter.dialect,
        resolved={queried_name: f"{source_name}.{measure_obj.name}"},
        guardrails_fired=fired,
        freshness=[_freshness(sources_by_name[s]) for s in used_sources],
        warnings=[],
        partial_additive=PartialAdditiveMetadata(
            kind="semi_additive",
            collapse_dimension=sa.collapse_dimension,
            collapse_agg=str(sa.collapse_agg),
            collapsed=collapsed,
        ),
    )


def _build_semi_additive(
    owner: str,
    measure: Measure,
    metric_name: str,
    collapse_alias: str,
    collapse_dim: Dimension,
    dimensions: list[tuple[str, Dimension]],
    where_conditions: list[exp.Expression],
    join_edges: list[JoinEdge],
    sources_by_name: dict[str, SemanticSource],
    collapse_agg: CollapseAgg,
) -> exp.Expression:
    """Emit the window or nested-aggregate SQL for a semi_additive collapse (SPEC §4.2).

    ``last``/``first`` → ROW_NUMBER() window CTE then filter rn = 1.
    ``avg``/``min``/``max`` → per_snapshot CTE with two-level GROUP BY.
    """
    collapse_col = exp.column(collapse_dim.column, table=collapse_alias)

    if collapse_agg in {CollapseAgg.LAST, CollapseAgg.FIRST}:
        order_dir = "DESC" if collapse_agg is CollapseAgg.LAST else "ASC"

        # Inner CTE: project grouped dimensions + raw input columns + ROW_NUMBER window.
        dim_names = _dimension_output_names(dimensions)
        inner = exp.Select()
        inner_projections: list[exp.Expression] = []
        partition_exprs: list[exp.Expression] = []
        for (src, dim), name in zip(dimensions, dim_names, strict=True):
            expr = _dimension_expr(src, dim)
            inner_projections.append(_alias(expr, name))
            partition_exprs.append(expr)

        for input_col in _input_columns(measure):
            inner_projections.append(_alias(exp.column(input_col, table=owner), input_col))

        order_item = cast(
            "exp.Expression",
            exp.Ordered(
                this=collapse_col,
                desc=order_dir == "DESC",
            ),
        )
        window_spec = cast(
            "exp.Expression",
            exp.Window(
                this=exp.RowNumber(),
                partition_by=partition_exprs,
                order=exp.Order(expressions=[order_item]),
            ),
        )
        inner_projections.append(_alias(window_spec, "rn"))
        inner = inner.select(*inner_projections)
        inner = _from_and_joins(inner, owner, join_edges, sources_by_name)
        if where_conditions:
            inner = inner.where(exp.and_(*where_conditions))

        # Outer SELECT: aggregate measure over ranked rows, filter rn = 1.
        _RANKED = "ranked"
        outer = exp.Select()
        outer_projections: list[exp.Expression] = []
        outer_group: list[exp.Expression] = []
        for name in dim_names:
            dim_col = cast("exp.Expression", exp.column(name, table=_RANKED))
            outer_projections.append(_alias(dim_col, name))
            outer_group.append(dim_col)
        outer_projections.append(_alias(_qualify_to(_parse(measure.expr), _RANKED), metric_name))
        outer = outer.select(*outer_projections)
        outer = outer.from_(exp.to_table(_RANKED))
        rn_filter = cast(
            "exp.Expression",
            exp.EQ(
                this=cast("exp.Expression", exp.column("rn", table=_RANKED)),
                expression=exp.Literal.number(1),
            ),
        )
        outer = outer.where(rn_filter)
        if outer_group:
            outer = outer.group_by(*outer_group)

        return cast("exp.Expression", outer.with_(_RANKED, as_=inner))

    # avg / min / max — nested GROUP BY form.
    _PER_SNAP = "per_snapshot"
    agg_fn = str(collapse_agg).upper()

    # Inner CTE: group by (grouped dims + collapse dim), compute measure per snapshot.
    dim_names = _dimension_output_names(dimensions)
    inner = exp.Select()
    inner_projections = []
    inner_group: list[exp.Expression] = []
    for (src, dim), name in zip(dimensions, dim_names, strict=True):
        expr = _dimension_expr(src, dim)
        inner_projections.append(_alias(expr, name))
        inner_group.append(expr)
    inner_projections.append(_alias(_measure_expr(owner, measure), "m"))
    inner_group.append(collapse_col)
    inner = inner.select(*inner_projections)
    inner = _from_and_joins(inner, owner, join_edges, sources_by_name)
    if where_conditions:
        inner = inner.where(exp.and_(*where_conditions))
    inner = inner.group_by(*inner_group)

    # Outer SELECT: apply agg_fn over the per-snapshot measure.
    outer = exp.Select()
    outer_projections = []
    outer_group = []
    for name in dim_names:
        dim_col = cast("exp.Expression", exp.column(name, table=_PER_SNAP))
        outer_projections.append(_alias(dim_col, name))
        outer_group.append(dim_col)
    m_col = cast("exp.Expression", exp.column("m", table=_PER_SNAP))
    outer_projections.append(_alias(_func(agg_fn, m_col), metric_name))
    outer = outer.select(*outer_projections)
    outer = outer.from_(exp.to_table(_PER_SNAP))
    if outer_group:
        outer = outer.group_by(*outer_group)

    return cast("exp.Expression", outer.with_(_PER_SNAP, as_=inner))


# --- Recompute-at-grain compile path (§4.3) ----------------------------------


def _compile_recompute_at_grain(
    query: SemanticQuery,
    binding: ResolverBinding,
    resolver: ContractResolver,
    sources_by_name: dict[str, SemanticSource],
    *,
    dialect: str = "postgres",
) -> CompileResult:
    """Compile a recompute_at_grain metric (distinct_count / percentile) from base rows (§4.3).

    Never derives from pre-aggregates: always groups the base table by the requested
    dimensions and computes the aggregate directly. Fanout policy is kind-specific:
    - distinct_count tolerates row duplication (DISTINCT dedups); LEFT joins preserve population.
    - percentile rejects any fanning join with FANOUT_UNSAFE (sort-based quantile is corrupted).
    """
    assert binding.recompute_at_grain is not None  # noqa: S101 — routing guarantees this kind
    rg = binding.recompute_at_grain
    adapter = adapter_for(dialect)
    queried_name = query.metrics[0]

    assert binding.source is not None  # noqa: S101 — enforced by model_validator
    source_name = binding.source
    alias_to_source = build_alias_tree(source_name, sources_by_name)

    # Stage 2 — dimensions & filters.
    dimensions = _resolve_dimensions(query, sources_by_name, source_name, alias_to_source)
    referenced = {alias for alias, _ in dimensions}
    where_conditions, filter_sources = _bind_filters(
        query.filters, sources_by_name, source_name, alias_to_source
    )
    referenced |= filter_sources
    referenced |= {source_name}

    # Stage 3 — join graph.
    join_edges = plan_joins(
        source_name, referenced - {source_name}, sources_by_name, via=list(query.via) or None
    )

    # Stage 4 — fanout safety floor (kind-specific, §4.3).
    fanout = any(edge.join.relationship in _FANOUT for edge in join_edges)
    if fanout and rg.kind is BindingKind.PERCENTILE:
        raise FanoutUnsafe(
            f"percentile metric {queried_name!r} cannot be used with a "
            f"one_to_many/many_to_many join; row duplication corrupts a sort-based quantile — "
            f"request it without the fanning dimension"
        )
    # distinct_count tolerates fanout: DISTINCT deduplicates multiplied rows; LEFT joins
    # (the only kind the compiler emits) do not change the population over which DISTINCT counts.

    # Resolve the referenced column to (alias, physical_column).
    col_name = rg.distinct_on if rg.kind is BindingKind.DISTINCT_COUNT else rg.column
    assert col_name is not None  # noqa: S101 — enforced by model_validator
    col_binding = _bind_name(col_name, sources_by_name, source_name, alias_to_source)
    if col_binding is None:
        raise UnreachableError(
            f"metric {queried_name!r}: {'distinct_on' if rg.kind is BindingKind.DISTINCT_COUNT else 'column'} "
            f"{col_name!r} is not declared on source {source_name!r} or any reachable join"
        )
    col_alias, col_phys = col_binding

    # population_filter — defines the population this metric is compiled over (§4.5); before guardrails.
    where_conditions += _population_filter_conditions(
        binding.binding.canonical.population_filter, sources_by_name, source_name, alias_to_source
    )

    # Stage 6 — guardrails: source-level guardrails keyed on (source, metric_name).
    # recompute_at_grain bindings have no declared measure; we use the metric name as the
    # measure key so source-wide guardrails (applies_to: { source }, measure: None) still fire.
    dummy_metric = _ResolvedMetric(
        name=queried_name,
        source=source_name,
        # Create a minimal Measure-like stand-in using the physical column.
        measure=_make_synthetic_measure(col_phys),
    )
    guard_conditions, fired = _enforce_guardrails(
        [dummy_metric], resolver, query.context, sources_by_name
    )
    where_conditions += guard_conditions

    # Stage 7 — emit SQL.
    ast = _build_recompute(
        owner=source_name,
        rg=rg,
        col_alias=col_alias,
        col_phys=col_phys,
        metric_name=queried_name,
        dimensions=dimensions,
        where_conditions=where_conditions,
        join_edges=join_edges,
        sources_by_name=sources_by_name,
        adapter=adapter,
    )
    sql = adapter.emit(ast, limit=query.limit)
    warnings: list[str] = []
    if rg.kind is BindingKind.PERCENTILE and not adapter.supports_percentile_cont():
        warnings.append(
            f"metric {queried_name!r}: dialect {adapter.dialect!r} has no native percentile "
            f"aggregate — computed via nearest-rank window function, which returns an actual "
            f"row value rather than a linearly interpolated one (differs from PERCENTILE_CONT "
            f"on even-sized groups)"
        )

    # Stage 8 — result metadata.
    used_sources = sorted({source_name} | {e.join.to for e in join_edges})
    return CompileResult(
        sql=sql,
        dialect=adapter.dialect,
        resolved={queried_name: f"recompute_at_grain({source_name}.{col_name})"},
        guardrails_fired=fired,
        freshness=[_freshness(sources_by_name[s]) for s in used_sources],
        warnings=warnings,
        recompute_at_grain=RecomputeAtGrainMetadata(
            kind=str(rg.kind),
            distinct_on=rg.distinct_on,
            column=rg.column,
            quantile=rg.quantile,
        ),
    )


def _compile_opaque(
    query: SemanticQuery,
    binding: ResolverBinding,
    resolver: ContractResolver,
    sources_by_name: dict[str, SemanticSource],
    *,
    dialect: str = "postgres",
) -> CompileResult:
    """Compile an opaque metric — serve at native grain only, no re-aggregation (§4.4).

    At native grain (requested dimensions == native_grain set) → direct lookup with no GROUP BY.
    Any other grain → UNSUPPORTED_MEASURE with a rationale.
    """
    assert binding.opaque is not None  # noqa: S101 — routing guarantees this kind
    opaque = binding.opaque
    adapter = adapter_for(dialect)
    queried_name = query.metrics[0]

    assert binding.source is not None and binding.measure is not None  # noqa: S101 — enforced by model_validator
    source_name = binding.source
    alias_to_source = build_alias_tree(source_name, sources_by_name)

    # Stage 2 — dimensions & filters.
    dimensions = _resolve_dimensions(query, sources_by_name, source_name, alias_to_source)
    where_conditions, filter_sources = _bind_filters(
        query.filters, sources_by_name, source_name, alias_to_source
    )
    referenced = {alias for alias, _ in dimensions} | filter_sources | {source_name}

    # Stage 3 — join graph.
    join_edges = plan_joins(
        source_name, referenced - {source_name}, sources_by_name, via=list(query.via) or None
    )

    # Grain guard (AC1): requested dimensions must exactly match native_grain (§4.4).
    requested_dims = {dim.name for _, dim in dimensions}
    native_dims = set(opaque.native_grain)
    if requested_dims != native_dims:
        native_repr = " × ".join(sorted(opaque.native_grain))
        raise UnsupportedMeasure(
            f"metric {queried_name!r} is opaque and can only be served at its native grain "
            f"({native_repr}); cannot re-aggregate a pre-computed value — "
            f"requested grain was {sorted(requested_dims)!r}"
        )

    # Resolve the measure object for guardrails and SQL emission.
    source = sources_by_name.get(source_name)
    if source is None:
        raise Unresolved(f"metric {queried_name!r} binds to unknown source {source_name!r}")
    measure = _find_measure(source, binding.measure)
    if measure is None:
        raise Unresolved(
            f"metric {queried_name!r} binds to unknown measure {source_name}.{binding.measure!r}"
        )
    resolved_metric = _ResolvedMetric(name=queried_name, source=source_name, measure=measure)

    # population_filter — defines the population this metric is about (§4.5); before guardrails.
    where_conditions += _population_filter_conditions(
        binding.binding.canonical.population_filter, sources_by_name, source_name, alias_to_source
    )

    # Stage 6 — guardrails.
    guard_conditions, fired = _enforce_guardrails(
        [resolved_metric], resolver, query.context, sources_by_name
    )
    where_conditions += guard_conditions

    # Stage 7 — emit raw lookup (no aggregate, no GROUP BY).
    ast = _build_opaque(
        owner=source_name,
        metric=resolved_metric,
        dimensions=dimensions,
        where_conditions=where_conditions,
        join_edges=join_edges,
        sources_by_name=sources_by_name,
    )
    sql = adapter.emit(ast, limit=query.limit)

    # Stage 8 — result metadata.
    used_sources = sorted({source_name} | {e.join.to for e in join_edges})
    return CompileResult(
        sql=sql,
        dialect=adapter.dialect,
        resolved={queried_name: f"opaque({source_name}.{binding.measure})"},
        guardrails_fired=fired,
        freshness=[_freshness(sources_by_name[s]) for s in used_sources],
        warnings=[],
        opaque=OpaqueMetadata(
            source=source_name,
            measure=binding.measure,
            native_grain=list(opaque.native_grain),
        ),
    )


def _build_opaque(
    owner: str,
    metric: _ResolvedMetric,
    dimensions: list[tuple[str, Dimension]],
    where_conditions: list[exp.Expression],
    join_edges: list[JoinEdge],
    sources_by_name: dict[str, SemanticSource],
) -> exp.Select:
    """Build a raw direct-lookup SELECT for an opaque metric — no aggregate, no GROUP BY (§4.4)."""
    select = exp.Select()
    projections: list[exp.Expression] = []
    for (src, dim), name in zip(dimensions, _dimension_output_names(dimensions), strict=True):
        projections.append(_alias(_dimension_expr(src, dim), name))
    projections.append(_alias(_measure_expr(metric.source, metric.measure), metric.measure.name))
    select = select.select(*projections)
    select = _from_and_joins(select, owner, join_edges, sources_by_name)
    if where_conditions:
        select = select.where(exp.and_(*where_conditions))
    return select


def _build_recompute(
    owner: str,
    rg: RecomputeAtGrainBinding,
    col_alias: str,
    col_phys: str,
    metric_name: str,
    dimensions: list[tuple[str, Dimension]],
    where_conditions: list[exp.Expression],
    join_edges: list[JoinEdge],
    sources_by_name: dict[str, SemanticSource],
    adapter: DialectAdapter,
) -> exp.Select:
    """Build the recompute-at-grain SELECT: group base table by dims, aggregate directly.

    distinct_count → COUNT(DISTINCT <col>)
    percentile     → PERCENTILE_CONT(q) WITHIN GROUP (ORDER BY <col>) where the dialect has
                      a native ordered-set aggregate; otherwise a CUME_DIST() window-function
                      fallback (see :func:`_build_percentile_fallback`).

    Never wraps in DISTINCT ON dedup — the grain is always recomputed from base rows.
    """
    if rg.kind is BindingKind.DISTINCT_COUNT:
        qualified_col = exp.column(col_phys, table=col_alias)
        agg_expr: exp.Expression = cast(
            "exp.Expression",
            exp.Count(this=exp.Distinct(expressions=[qualified_col])),
        )
    elif not adapter.supports_percentile_cont():
        assert rg.quantile is not None  # noqa: S101 — enforced by model_validator
        return _build_percentile_fallback(
            owner=owner,
            quantile=rg.quantile,
            col_alias=col_alias,
            col_phys=col_phys,
            metric_name=metric_name,
            dimensions=dimensions,
            where_conditions=where_conditions,
            join_edges=join_edges,
            sources_by_name=sources_by_name,
        )
    else:
        # PERCENTILE: parse PERCENTILE_CONT(q) WITHIN GROUP (ORDER BY col), then qualify.
        assert rg.quantile is not None  # noqa: S101 — enforced by model_validator
        agg_expr = _qualify_to(
            _parse(f"PERCENTILE_CONT({rg.quantile}) WITHIN GROUP (ORDER BY {col_phys})"),
            col_alias,
        )

    select = exp.Select()
    projections: list[exp.Expression] = []
    group_exprs: list[exp.Expression] = []
    for (src, dim), name in zip(dimensions, _dimension_output_names(dimensions), strict=True):
        expr = _dimension_expr(src, dim)
        projections.append(_alias(expr, name))
        group_exprs.append(expr)
    projections.append(_alias(agg_expr, metric_name))
    select = select.select(*projections)
    select = _from_and_joins(select, owner, join_edges, sources_by_name)
    if where_conditions:
        select = select.where(exp.and_(*where_conditions))
    if group_exprs:
        select = select.group_by(*group_exprs)
    return select


def _build_percentile_fallback(
    owner: str,
    quantile: float,
    col_alias: str,
    col_phys: str,
    metric_name: str,
    dimensions: list[tuple[str, Dimension]],
    where_conditions: list[exp.Expression],
    join_edges: list[JoinEdge],
    sources_by_name: dict[str, SemanticSource],
) -> exp.Select:
    """Build a percentile recompute for dialects without an ordered-set aggregate (e.g. SQLite).

    Ranks each row within its dimension partition via ``CUME_DIST()`` and picks the smallest
    value at or past the target quantile — the standard window-function substitute for
    engines with no ``PERCENTILE_CONT() WITHIN GROUP`` support. This is a nearest-rank
    (percentile_disc-style) result: for even-sized groups it returns an actual row value
    rather than the linear interpolation between the two middle values that PERCENTILE_CONT
    would produce.
    """
    _RANKED = "_ranked"
    _VAL = "_val"
    _CD = "_cd"

    col_expr = exp.column(col_phys, table=col_alias)

    dim_names = _dimension_output_names(dimensions)
    inner = exp.Select()
    inner_projections: list[exp.Expression] = []
    partition_exprs: list[exp.Expression] = []
    for (src, dim), name in zip(dimensions, dim_names, strict=True):
        expr = _dimension_expr(src, dim)
        inner_projections.append(_alias(expr, name))
        partition_exprs.append(expr)
    inner_projections.append(_alias(col_expr, _VAL))
    cume_dist = cast(
        "exp.Expression",
        exp.Window(
            this=exp.CumeDist(),
            partition_by=partition_exprs,
            order=exp.Order(expressions=[exp.Ordered(this=col_expr)]),
        ),
    )
    inner_projections.append(_alias(cume_dist, _CD))
    inner = inner.select(*inner_projections)
    inner = _from_and_joins(inner, owner, join_edges, sources_by_name)
    if where_conditions:
        inner = inner.where(exp.and_(*where_conditions))

    outer = exp.Select()
    outer_projections: list[exp.Expression] = []
    outer_group: list[exp.Expression] = []
    for name in dim_names:
        dim_col = cast("exp.Expression", exp.column(name, table=_RANKED))
        outer_projections.append(_alias(dim_col, name))
        outer_group.append(dim_col)
    val_col = cast("exp.Expression", exp.column(_VAL, table=_RANKED))
    outer_projections.append(_alias(_func("MIN", val_col), metric_name))
    outer = outer.select(*outer_projections)
    outer = outer.from_(exp.to_table(_RANKED))
    cd_col = cast("exp.Expression", exp.column(_CD, table=_RANKED))
    outer = outer.where(exp.GTE(this=cd_col, expression=exp.Literal.number(quantile)))
    if outer_group:
        outer = outer.group_by(*outer_group)

    return outer.with_(_RANKED, as_=inner)


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


# --- Stage 2 -----------------------------------------------------------------


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


# --- Stage 6 -----------------------------------------------------------------


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
) -> exp.Select:
    """Single-SELECT emission for the no-fanout case (SPEC §4 step 7)."""
    select = exp.Select()
    projections: list[exp.Expression] = []
    group_exprs: list[exp.Expression] = []
    for (src, dim), name in zip(dimensions, _dimension_output_names(dimensions), strict=True):
        expr = _dimension_expr(src, dim)
        projections.append(_alias(expr, name))
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


# --- shared helpers ----------------------------------------------------------


def _find_measure(source: SemanticSource, name: str) -> Measure | None:
    return next((m for m in source.measures if m.name == name), None)


def _make_synthetic_measure(col_name: str) -> Measure:
    """Build a minimal non-additive Measure for guardrail enforcement in recompute_at_grain.

    recompute_at_grain bindings reference a column, not a declared measure. Source-wide
    guardrails (applies_to: { source }, measure: None) match on source alone, so the
    synthetic name never reaches any equality check that would cause a false positive.
    """
    return Measure(name=col_name, expr=col_name, additivity=Additivity.NON_ADDITIVE)


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


_RELATED_CAP = 5


def _related(
    queried_sources: set[str],
    queried_metric_names: set[str],
    query: SemanticQuery,
    resolver: ContractResolver,
    sources_by_name: dict[str, SemanticSource],
) -> RelatedMetadata:
    """Compute related-query suggestions for Stage 8 metadata (SPEC-E7/E8 §2.2)."""
    used_dims: set[str] = set(query.dimensions)
    filter_tokens: set[str] = {tok for f in query.filters for tok in f.split()}

    alias_to_src: dict[str, str] = {}
    for src_name in queried_sources:
        alias_to_src.update(build_alias_tree(src_name, sources_by_name))
    dim_label_lookup: dict[tuple[str, str], str | None] = {
        (sn, d.name): d.label for sn, src in sources_by_name.items() for d in src.dimensions
    }

    seen_dims: set[str] = set()
    raw_dims: list[RelatedDimension] = []
    for src_name in sorted(queried_sources):
        for entry_name, alias in reachable_dimension_names(src_name, sources_by_name):
            bare = entry_name.split(".")[-1]
            if entry_name in used_dims or bare in used_dims or bare in filter_tokens:
                continue
            if entry_name not in seen_dims:
                seen_dims.add(entry_name)
                actual_src = alias_to_src.get(alias, alias)
                label = dim_label_lookup.get((actual_src, bare))
                raw_dims.append(RelatedDimension(name=entry_name, source=alias, label=label))
    unused_dimensions = sorted(raw_dims, key=lambda d: (d.name, d.source))[:_RELATED_CAP]

    seen_metrics: set[str] = set()
    raw_metrics: list[RelatedMetric] = []
    for src_name in sorted(queried_sources):
        for metric_name in resolver.metrics_for_source(src_name):
            if metric_name not in queried_metric_names and metric_name not in seen_metrics:
                seen_metrics.add(metric_name)
                raw_metrics.append(RelatedMetric(name=metric_name, source=src_name))
    sibling_metrics = sorted(raw_metrics, key=lambda m: m.name)[:_RELATED_CAP]

    return RelatedMetadata(
        unused_dimensions=unused_dimensions,
        sibling_metrics=sibling_metrics,
    )


# --- Stage 5 helpers ---------------------------------------------------------


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

            from canonic.contracts.finality import evaluate_watermark

            watermark_dt = evaluate_watermark(final_r.watermark, final_r.tz, query.as_of)
            time_names = _time_column_names(rule, sources_by_name)
            if _window_exceeds_watermark(query.filters, time_names, watermark_dt, sources_by_name):
                logger.warning(
                    "restrict_source enforced: guardrail=%s watermark exceeded", guardrail.id
                )
                raise GuardrailBlock(guardrail.rationale)
