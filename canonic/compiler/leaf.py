"""Leaf planning — stages 2-6 for one independently-aggregated sub-query (SPEC-E5-E15 §4).

A *leaf* is one source aggregated to the requested dimensions: its own dimension binding,
join plan, fanout analysis, finality selection, and guardrail enforcement. It is the unit
the compose step assembles into a single statement, and the unit every compile path is
built from — a ratio's numerator and denominator, a group of metrics sharing one source,
and (once ported) a semi-additive collapse or a recompute-at-grain aggregate are all leaves.

This module exists because :func:`canonic.compiler.strategies.composite._plan_leaf` and
:func:`canonic.compiler.strategies.simple_additive._plan_metric_group` had grown into
line-for-line copies of the same six stages, differing only in how many metrics they
project and whether they consider finality. Two copies of a safety floor is one copy too
many: a fanout rule fixed in one and missed in the other is a silently wrong number.
"""

from __future__ import annotations

import contextlib
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, cast

from canonic.compiler._helpers import (
    _FANOUT,
    _bind_filters,
    _build_deduped,
    _build_finality_union,
    _build_simple,
    _dimension_expr,
    _dimension_output_names,
    _enforce_guardrails,
    _find_time_dim_name,
    _guardrail_join_sources,
    _measure_expr,
    _population_filter_conditions,
    _resolve_dim_mask,
    _resolve_dimensions,
    _tenant_conditions,
)
from canonic.compiler.joins import build_alias_tree, plan_joins
from canonic.compiler.result import FinalityMetadata
from canonic.exc import Ambiguous, AmbiguousJoinPath, FanoutUnsafe, UnsupportedMeasure
from canonic.exc import Unreachable as UnreachableError
from canonic.semantic.models import Additivity

if TYPE_CHECKING:
    from collections.abc import Iterator, Sequence

    from sqlglot import exp

    from canonic.compiler._helpers import DimMask, _ResolvedMetric
    from canonic.compiler.joins import JoinEdge
    from canonic.compiler.query import SemanticQuery
    from canonic.compiler.result import FiredGuardrail
    from canonic.contracts.principal import EffectivePolicy, Principal
    from canonic.contracts.resolver import ContractResolver
    from canonic.semantic.models import Dimension, SemanticSource

__all__ = [
    "AuxCte",
    "LeafBuilder",
    "LeafContext",
    "LeafInputs",
    "LeafKey",
    "LeafMetric",
    "LeafPlan",
    "plan_leaf",
]


@dataclass(frozen=True, slots=True)
class LeafContext:
    """Everything stages 2-6 need that does not vary from one leaf to the next.

    ``principal`` and ``effective_policy`` are bound once in
    :func:`canonic.compiler.pipeline.compile`'s stage 0 and carried here so every leaf sees
    them without its own signature change (SPEC-E12 §3) — ``plan_leaf`` reads them straight
    off ``ctx`` to inject stage 2b's tenant predicates.
    """

    query: SemanticQuery
    resolver: ContractResolver
    sources_by_name: dict[str, SemanticSource]
    principal: Principal
    effective_policy: EffectivePolicy


@dataclass(frozen=True, slots=True)
class LeafMetric:
    """One metric to project from a leaf, with the scoping that belongs to it alone.

    ``population_filter`` is the metric's own declared population (SPEC-fuller-E15 §4.5),
    already combined with any enclosing composite's filter by the caller. ``alias``
    overrides the output column name, which the composite path uses so a component's
    column can be referenced by a fixed name in the compose expression.
    """

    resolved: _ResolvedMetric
    population_filter: str | None = None
    alias: str | None = None


def _output_alias(leaf_metric: LeafMetric) -> str:
    return leaf_metric.alias or leaf_metric.resolved.measure.name


@dataclass(frozen=True, slots=True)
class AuxCte:
    """An inner CTE a leaf builder needs, lifted out so it can be declared alongside it.

    ``semi_additive`` ranks rows in a ``ranked`` CTE and ``recompute`` ranks them in
    ``_ranked``. Left where they were built, those would end up as a ``WITH`` nested inside
    a leaf's own CTE body once the leaf became one of several — legal on Postgres and
    DuckDB, not on Redshift. Hoisting them into the single outer ``WITH`` under a
    leaf-scoped name keeps one statement shape for every dialect (AMENDMENT §3.5).
    """

    name: str
    body: exp.Expression


@dataclass(frozen=True, slots=True)
class LeafInputs:
    """The resolved plan stages 2-6 produce, handed to a builder to turn into a SELECT."""

    ctx: LeafContext
    owner: str
    dimensions: list[tuple[str, Dimension]]
    where_conditions: list[exp.Expression]
    join_edges: list[JoinEdge]
    fanout: bool
    alias_to_source: dict[str, str]
    name_prefix: str = ""
    #: A role's masking rules resolved against this leaf's join aliases (SPEC-E12 §1.2,
    #: Phase 7), keyed the same way :func:`_dimension_expr`'s callers already hold a
    #: ``(src, dim.column)`` pair.
    dim_mask: DimMask = field(default_factory=dict)

    @property
    def dim_names(self) -> list[str]:
        return _dimension_output_names(self.dimensions)


#: Turns a resolved plan plus a measure list into a SELECT and any CTEs it needs.
LeafBuilder = Callable[
    [LeafInputs, "Sequence[LeafMetric]"], "tuple[exp.Expression, tuple[AuxCte, ...]]"
]


#: Rendering dialect for every string that goes into a :class:`LeafKey`. Fixed on purpose
#: and independent of the query's target dialect: the key is an internal identity, not
#: emitted SQL, and two leaves must compare equal or not for reasons that have nothing to
#: do with which warehouse the statement is bound for.
_KEY_DIALECT = "postgres"


def _render(expr: exp.Expression) -> str:
    return expr.sql(dialect=_KEY_DIALECT, identify=True)


@dataclass(frozen=True, slots=True)
class LeafKey:
    """Canonical identity of a leaf's *resolved* plan (AMENDMENT §3.2).

    Two leaves may share a CTE only when these fields are equal, and two leaves may be
    *fused* into one CTE projecting both measures when everything but the measure is
    equal. Getting that boundary wrong is the failure mode this whole design has to
    avoid: a key that is too loose merges two leaves whose rows genuinely differ, and
    both metrics then report the same plausible, wrong number. So every input that can
    change which rows a leaf emits appears here, rendered to a string rather than
    compared structurally, and nothing is sorted that the emitted SQL keeps in order.

    The key doubles as the sort key that assigns ``_leaf_<i>`` names, which is what makes
    a repeated compile byte-identical (§6). It is built only from resolved plan data, so
    it never depends on iteration order, object identity, or a hash seed.
    """

    source: str
    strategy: str
    dimensions: tuple[str, ...]
    dimension_exprs: tuple[str, ...]
    filters: tuple[str, ...]
    join_path: tuple[tuple[str, str, str], ...]
    finality: tuple[str, ...]
    strategy_params: tuple[tuple[str, str], ...]
    measures: tuple[tuple[str, str], ...]

    @property
    def fusion_key(self) -> tuple[object, ...]:
        """Everything except the measures — the identity a fused CTE would carry."""
        return (
            self.source,
            self.strategy,
            self.dimensions,
            self.dimension_exprs,
            self.filters,
            self.join_path,
            self.finality,
            self.strategy_params,
        )

    @property
    def sort_key(self) -> tuple[object, ...]:
        return (*self.fusion_key, self.measures)


@dataclass(frozen=True, slots=True)
class LeafPlan:
    """A planned leaf: the SELECT plus everything compose and stage 8 need.

    ``rebuild`` re-emits this leaf's SELECT projecting a different measure list, reusing
    the plan already computed rather than re-deriving it. That is what makes fusion
    trustworthy: a fused CTE is the same WHERE, the same joins, and the same GROUP BY as
    each of its constituents, by construction rather than by a second planning pass that
    could drift.
    """

    key: LeafKey
    select: exp.Expression
    metrics: tuple[LeafMetric, ...]
    dim_names: tuple[str, ...]
    measure_aliases: tuple[str, ...]
    used_sources: frozenset[str]
    rebuild: Callable[[Sequence[LeafMetric], str], tuple[exp.Expression, tuple[AuxCte, ...]]]
    aux: tuple[AuxCte, ...] = ()
    fired: tuple[FiredGuardrail, ...] = ()
    warnings: tuple[str, ...] = ()
    finality: FinalityMetadata | None = None
    projects_is_final: bool = False
    #: Sources that received a tenant predicate / were declared tenant-neutral for this leaf
    #: (SPEC-E12 §3 stage 8). Derived from the predicates actually emitted, not from the
    #: policy, so S16 AC3 ("lists exactly the sources that received a predicate") holds.
    scoped_sources: frozenset[str] = frozenset()
    shared_sources: frozenset[str] = frozenset()
    #: Whether another leaf's measure may be merged into this CTE. False for the kinds
    #: whose builder emits a shape around exactly one measure (a ROW_NUMBER collapse, an
    #: ordered-set quantile): merging a second measure in would silently change what the
    #: window or the grain lock applies to. Such leaves still deduplicate against an
    #: identical leaf, they just never absorb a *different* measure.
    fusable: bool = True


@dataclass(slots=True)
class _Scoping:
    """The leaf's effective WHERE, plus the guardrails that contributed to it."""

    where_conditions: list[exp.Expression]
    fired: list[FiredGuardrail] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def _apply_metric_scoping(
    ctx: LeafContext,
    owner: str,
    metrics: Sequence[LeafMetric],
    where_conditions: list[exp.Expression],
    alias_to_source: dict[str, str],
) -> _Scoping:
    """Fold the leaf's population_filter and guardrails into one shared WHERE (§4.5, stage 6).

    Every metric on a leaf shares that WHERE, which is only sound because compose fuses
    metrics onto one leaf exactly when their planned filters are byte-identical (see
    :class:`LeafKey`) — metrics whose filters differ get their own leaf. Conditions are
    therefore taken from the first metric, which the fusion key guarantees is every
    metric's. Guardrail *identity* can still differ where two guardrails happen to carry
    the same predicate, so ``fired`` and ``warnings`` union across all of them.
    """
    resolver, query, sources_by_name = ctx.resolver, ctx.query, ctx.sources_by_name
    conditions = list(where_conditions)
    conditions += _population_filter_conditions(
        metrics[0].population_filter, sources_by_name, owner, alias_to_source
    )

    scoping = _Scoping(where_conditions=conditions)
    fired_seen: set[str] = set()
    warnings_seen: set[str] = set()
    for i, leaf_metric in enumerate(metrics):
        guard = _enforce_guardrails(
            [leaf_metric.resolved], resolver, query.context, sources_by_name
        )
        if i == 0:
            scoping.where_conditions += guard.conditions
        for g in guard.fired:
            if g.id not in fired_seen:
                fired_seen.add(g.id)
                scoping.fired.append(g)
        for w in guard.warnings:
            if w not in warnings_seen:
                warnings_seen.add(w)
                scoping.warnings.append(w)
    return scoping


@contextlib.contextmanager
def _naming_the_leaf(metrics: Sequence[LeafMetric], owner: str) -> Iterator[None]:
    """Re-raise a binding failure with the leaf that could not bind it (AMENDMENT §5).

    Every requested dimension and query-level filter must resolve against *every* leaf,
    and the whole query fails if one cannot. Which leaf failed is the actionable half of
    that message: "dimension 'region' is not declared on any reachable source" sends the
    caller looking at the wrong model when the truth is that ``region`` is fine for
    revenue and simply does not exist for the shipment metric they also asked for.

    The error code is deliberately unchanged. UNREACHABLE already means exactly this, and
    the amendment adds no code to the frozen registry.
    """
    try:
        yield
    except AmbiguousJoinPath as exc:
        raise AmbiguousJoinPath(
            _scoped_message(exc, metrics, owner),
            owner=exc.owner,
            target=exc.target,
            candidates=exc.candidates,
        ) from exc
    except (UnreachableError, Ambiguous) as exc:
        raise type(exc)(_scoped_message(exc, metrics, owner), candidates=exc.candidates) from exc


def _scoped_message(exc: Exception, metrics: Sequence[LeafMetric], owner: str) -> str:
    names = ", ".join(repr(m.resolved.name) for m in metrics)
    return f"metric {names} (leaf {owner!r}): {exc}"


def _enforce_safety_floor(
    resolved: Sequence[_ResolvedMetric],
    *,
    fanout: bool,
    grouped: set[str],
) -> None:
    """Stage 4 — refuse aggregations a join would corrupt (SPEC-fuller-E15 §5).

    Additive measures survive any join because the caller deduplicates the owner grain
    before aggregating. Anything else is corrupted by row multiplication, and a
    semi-additive measure is corrupted by collapsing across the dimension it is only
    additive within, so both refuse rather than return a plausible wrong number.
    """
    for m in resolved:
        add = m.measure.additivity
        if add is Additivity.ADDITIVE:
            if not m.measure.is_p0_compilable:
                raise UnsupportedMeasure(
                    f"measure {m.source}.{m.measure.name!r} uses an aggregate function "
                    f"not supported at P0"
                )
            continue
        if fanout:
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


def _build_additive(
    inputs: LeafInputs, metrics: Sequence[LeafMetric]
) -> tuple[exp.Expression, tuple[AuxCte, ...]]:
    """Default builder: enforce the additive safety floor, then aggregate at the grain.

    A fanning join multiplies the measure source's rows, so an additive aggregate over
    them would inflate. The floor above has already refused every non-additive measure in
    that situation, which is what makes deduplicating the owner grain the right answer
    rather than a guess.
    """
    resolved = [lm.resolved for lm in metrics]
    _enforce_safety_floor(
        resolved,
        fanout=inputs.fanout,
        grouped={dim.name for _alias, dim in inputs.dimensions},
    )
    emit = _build_deduped if inputs.fanout else _build_simple
    return (
        emit(
            inputs.owner,
            resolved,
            inputs.dimensions,
            inputs.where_conditions,
            inputs.join_edges,
            inputs.ctx.sources_by_name,
            measure_aliases=[_output_alias(lm) for lm in metrics],
            dim_mask=inputs.dim_mask,
        ),
        (),
    )


def plan_leaf(
    ctx: LeafContext,
    owner: str,
    metrics: Sequence[LeafMetric],
    *,
    finality_metric: str | None = None,
    strategy: str = "single",
    strategy_params: tuple[tuple[str, str], ...] = (),
    builder: LeafBuilder | None = None,
    fusable: bool = True,
) -> LeafPlan:
    """Run stages 2-6 for one leaf rooted at ``owner`` and build its SELECT.

    Every metric in ``metrics`` must already be bound to ``owner`` — resolving a metric
    name to its source and measure is stage 1, and stays with the caller so its error
    messages can name what the caller was actually resolving.

    ``finality_metric``, when given, is the metric name whose finality rule governs this
    leaf. If that rule exists and the query groups by a time dimension, the leaf is
    emitted as a ``UNION ALL`` over realizations projecting ``is_final``, so the compose
    step can apply the conservative merge across leaves (SPEC-fuller-E15 §7). Passing
    ``None`` skips stage 5 entirely.

    ``builder`` overrides how the SELECT is emitted for kinds that need a shape of their
    own — a ROW_NUMBER collapse, an ordered-set quantile, a grain-locked lookup. It also
    takes over the stage-4 safety floor, because what counts as a corrupting join is
    kind-specific: a percentile is destroyed by row duplication where a distinct count
    shrugs it off. The default builder is the additive one.
    """
    query, resolver, sources_by_name = ctx.query, ctx.resolver, ctx.sources_by_name
    resolved = [lm.resolved for lm in metrics]
    aliases = [_output_alias(lm) for lm in metrics]

    # Stage 2 — dimensions and filters, relative to this leaf's own owner. A dimension or
    # filter that will not bind here fails the whole query rather than being applied to
    # the leaves where it happens to work: half-applying a filter produces a result whose
    # columns were computed over different populations, with nothing saying so (§5).
    alias_to_source = build_alias_tree(owner, sources_by_name)
    dim_mask = _resolve_dim_mask(alias_to_source, ctx.effective_policy.masking)
    with _naming_the_leaf(metrics, owner):
        dimensions = _resolve_dimensions(query, sources_by_name, owner, alias_to_source)
        referenced = {alias for alias, _ in dimensions}
        where_conditions, filter_sources = _bind_filters(
            query.filters, sources_by_name, owner, alias_to_source
        )
    referenced |= filter_sources
    referenced |= {owner}
    referenced |= _guardrail_join_sources(
        [(m.source, m.measure.name) for m in resolved],
        resolver,
        query.context,
        sources_by_name,
        alias_to_source,
    )

    # Stage 3 — join graph from this leaf's owner to every alias it references.
    with _naming_the_leaf(metrics, owner):
        join_edges = plan_joins(
            owner, referenced - {owner}, sources_by_name, via=list(query.via) or None
        )

    fanout = any(edge.join.relationship in _FANOUT for edge in join_edges)

    # Stage 2b — tenant predicates, injected before query filters so a caller-supplied
    # filter can never widen what the principal's tenant already narrowed it to (§3, S14).
    tenant_scoping = _tenant_conditions(
        resolver,
        owner,
        join_edges,
        alias_to_source,
        ctx.principal.tenant,
        ctx.effective_policy,
    )

    # population_filter defines the population this leaf is compiled over (§4.5), and is
    # applied before guardrails so a guardrail narrows an already-scoped population.
    scoping = _apply_metric_scoping(
        ctx, owner, metrics, [*tenant_scoping.conditions, *where_conditions], alias_to_source
    )
    scoping.warnings = [*tenant_scoping.warnings, *scoping.warnings]

    # Stage 5 — finality, evaluated after guardrails so every WHERE condition the leaf
    # carries is re-qualified onto each realization branch.
    finality_rule = resolver.finality_for(finality_metric) if finality_metric else None
    time_dim_name: str | None = None
    if finality_rule is not None:
        time_dim_name = _find_time_dim_name(dimensions, sources_by_name, alias_to_source)
        if time_dim_name is None:
            finality_rule = None  # no time dimension → all rows implicitly final

    inputs = LeafInputs(
        ctx=ctx,
        owner=owner,
        dimensions=dimensions,
        where_conditions=scoping.where_conditions,
        join_edges=join_edges,
        fanout=fanout,
        alias_to_source=alias_to_source,
        dim_mask=dim_mask,
    )

    # Stage 7 (per leaf) — build the SELECT. Kept as a closure so compose can re-emit this
    # same plan with a longer measure list when it fuses leaves, or under a name prefix
    # when its inner CTEs have to be hoisted alongside it.
    leaf_finality: FinalityMetadata | None = None
    finality_key: tuple[str, ...] = ()
    if builder is None and finality_rule is not None and time_dim_name is not None:
        from canonic.contracts.finality import evaluate_watermark, watermark_to_iso

        rule, time_dim = finality_rule, time_dim_name
        final_r = next(r for r in rule.realizations if r.role == "final")
        watermark_dt = evaluate_watermark(
            cast("str", final_r.watermark), cast("str", final_r.tz), query.as_of
        )

        def build(
            leaf_metrics: Sequence[LeafMetric], name_prefix: str = ""
        ) -> tuple[exp.Expression, tuple[AuxCte, ...]]:
            return (
                _build_finality_union(
                    rule=rule,
                    query_metrics=[lm.resolved for lm in leaf_metrics],
                    dimensions=dimensions,
                    where_conditions=scoping.where_conditions,
                    sources_by_name=sources_by_name,
                    watermark_dt=watermark_dt,
                    time_dim_name=time_dim,
                    original_owner=owner,
                    measure_aliases=[_output_alias(lm) for lm in leaf_metrics],
                    masking=ctx.effective_policy.masking,
                ),
                (),
            )

        leaf_finality = FinalityMetadata(
            watermark=watermark_to_iso(watermark_dt),
            sources_used=[r.source for r in rule.realizations],
            result_flag=rule.result_flag or "per_row",
        )
        finality_key = (
            leaf_finality.watermark,
            *sorted(leaf_finality.sources_used),
            leaf_finality.result_flag or "",
        )
        used_sources = {r.source for r in rule.realizations}
    else:
        emit = builder or _build_additive

        def build(
            leaf_metrics: Sequence[LeafMetric], name_prefix: str = ""
        ) -> tuple[exp.Expression, tuple[AuxCte, ...]]:
            return emit(
                LeafInputs(
                    ctx=inputs.ctx,
                    owner=inputs.owner,
                    dimensions=inputs.dimensions,
                    where_conditions=inputs.where_conditions,
                    join_edges=inputs.join_edges,
                    fanout=inputs.fanout,
                    alias_to_source=inputs.alias_to_source,
                    dim_mask=inputs.dim_mask,
                    name_prefix=name_prefix,
                ),
                leaf_metrics,
            )

        used_sources = {owner} | {e.join.to for e in join_edges}

    select, aux = build(metrics, "")

    key = LeafKey(
        source=owner,
        strategy=strategy,
        dimensions=tuple(_dimension_output_names(dimensions)),
        dimension_exprs=tuple(
            _render(_dimension_expr(src, dim, dim_mask.get((src, dim.column))))
            for src, dim in dimensions
        ),
        filters=tuple(_render(c) for c in scoping.where_conditions),
        join_path=tuple((e.from_alias, e.alias, e.on_sql) for e in join_edges),
        finality=finality_key,
        strategy_params=strategy_params,
        measures=tuple(
            (m.measure.name, _render(_measure_expr(m.source, m.measure))) for m in resolved
        ),
    )

    return LeafPlan(
        key=key,
        select=select,
        aux=aux,
        fusable=fusable,
        metrics=tuple(metrics),
        dim_names=tuple(_dimension_output_names(dimensions)),
        measure_aliases=tuple(aliases),
        used_sources=frozenset(used_sources),
        rebuild=build,
        fired=tuple(scoping.fired),
        warnings=tuple(scoping.warnings),
        finality=leaf_finality,
        projects_is_final=leaf_finality is not None,
        scoped_sources=tenant_scoping.scoped_sources,
        shared_sources=tenant_scoping.shared_sources,
    )
