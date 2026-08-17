"""The deterministic compiler pipeline (SPEC-E5-E15 §4, stages 1–4, 6–8).

``compile`` turns a :class:`SemanticQuery` into dialect-correct, read-only SQL plus
result metadata. No LLM, no wall-clock, no randomness: identical inputs yield
byte-identical SQL (SPEC §8). The :class:`ContractResolver` is the only authority on
canonicality — the compiler trusts its results and never reimplements them (§6)."""

from __future__ import annotations

import contextlib
import logging
from typing import TYPE_CHECKING, Any

from canonic.compiler._helpers import _block_or_warn, _find_dimension, _query_references_dimension
from canonic.compiler.compose import MetricLeaves, MetricPlan, compose
from canonic.compiler.dialect import adapter_for
from canonic.compiler.joins import build_alias_tree, reachable_dimension_names
from canonic.compiler.result import (
    CompileResult,
    RelatedDimension,
    RelatedMetadata,
    RelatedMetric,
    ScopeMetadata,
    TrustInput,
)
from canonic.contracts.models import BindingKind, OnMissingPrincipal
from canonic.contracts.principal import Principal
from canonic.contracts.resolver import Ambiguous as ResolverAmbiguous
from canonic.contracts.resolver import Binding as ResolverBinding
from canonic.contracts.resolver import Unresolved as ResolverUnresolved
from canonic.exc import Ambiguous, TenantUnresolved, Unresolved
from canonic.trust.models import TrustTier, tier_meets
from canonic.trust.scorer import TrustScorer
from canonic.trust.signals import static_signals_for

if TYPE_CHECKING:
    from collections.abc import Mapping

    from canonic.compiler.leaf import LeafPlan
    from canonic.compiler.query import SemanticQuery
    from canonic.contracts.principal import EffectivePolicy
    from canonic.contracts.resolver import ContractResolver
    from canonic.semantic.models import SemanticSource

from canonic.compiler.strategies import (
    plan_composite,
    plan_opaque,
    plan_recompute_at_grain,
    plan_semi_additive,
    plan_simple_additive,
)
from canonic.compiler.strategies.simple_additive import _bind_metric, _enforce_restrict_source

logger = logging.getLogger(__name__)


__all__ = ["compile"]


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
                binding=binding.resolved_key,
            )
        )
    return inputs


def _enforce_min_trust(
    raw_bindings: list[tuple[str, ResolverBinding]],
    resolver: ContractResolver,
    context: str | None,
    trust_inputs: list[TrustInput],
) -> list[str]:
    """Stage 6b: block or warn when a min_trust guardrail's floor is not met (SPEC-E14 §7).

    Enforced from the static signal set only (provenance, assertion coverage) — the signals
    known before SQL is generated. Only metrics with a single resolved (source, measure) are
    matched (SINGLE/SEMI_ADDITIVE/OPAQUE kinds); composite (ratio/weighted_avg) and
    recompute_at_grain metrics have no single source/measure pair to match against
    ``applies_to``, the same limitation ``restrict_source`` already has. ``severity: error``
    (the default) raises GuardrailBlock; ``severity: warn`` returns a warning line instead.
    """
    warnings: list[str] = []
    if context is None:
        return warnings
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
                warnings.append(_block_or_warn(guardrail))
    return warnings


def _enforce_required_dimension(
    query: SemanticQuery,
    raw_bindings: list[tuple[str, ResolverBinding]],
    resolver: ContractResolver,
    sources_by_name: dict[str, SemanticSource],
) -> list[str]:
    """Stage 6c: block or warn when a required_dimension's dimension is neither grouped by
    nor filtered on (SPEC-E5-E15 §9 S9).

    Only metrics with a single resolved (source, measure) are matched (SINGLE/SEMI_ADDITIVE/
    OPAQUE kinds); composite (ratio/weighted_avg) and recompute_at_grain metrics have no
    single source/measure pair to match against ``applies_to``, the same limitation
    ``restrict_source`` and ``min_trust`` already have. Unlike those two, ``context`` is
    optional for this kind: a guardrail with no declared context applies to every query,
    including one with no ``query.context`` at all (see
    :meth:`ContractResolver.required_dimension_for`). ``severity: error`` (the default)
    raises GuardrailBlock; ``severity: warn`` returns a warning line instead.
    """
    warnings: list[str] = []
    used_dims = set(query.dimensions)
    filter_tokens = {tok for f in query.filters for tok in f.split()}
    seen: set[str] = set()
    for _name, binding in raw_bindings:
        if binding.source is None or binding.measure is None:
            continue
        for guardrail in resolver.required_dimension_for(
            binding.source, binding.measure, query.context
        ):
            assert guardrail.dimension is not None  # noqa: S101 — enforced by model_validator
            alias_to_source = build_alias_tree(binding.source, sources_by_name)
            found = _find_dimension(
                guardrail.dimension,
                sources_by_name,
                owner=binding.source,
                alias_to_source=alias_to_source,
            )
            satisfied = False
            if found is not None:
                alias, dim = found
                for cand in (dim.name, *dim.aliases):
                    entry_name = cand if alias == binding.source else f"{alias}.{cand}"
                    if _query_references_dimension(entry_name, used_dims, filter_tokens):
                        satisfied = True
                        break
            if satisfied or guardrail.id in seen:
                continue
            seen.add(guardrail.id)
            logger.warning(
                "required_dimension enforced: guardrail=%s dimension=%s missing",
                guardrail.id,
                guardrail.dimension,
            )
            warnings.append(_block_or_warn(guardrail, candidates=[guardrail.dimension]))
    return warnings


def _resolve_all_metrics(
    query: SemanticQuery,
    resolver: ContractResolver,
    effective_policy: EffectivePolicy,
) -> list[tuple[str, ResolverBinding]]:
    """Stage 1 — resolve every requested metric before planning anything (AMENDMENT §3.1).

    Failure is fail-fast for the whole query, never a partial result: a caller handed some
    columns and some errors has no reliable way to tell which numbers to trust, which is
    worse than being handed nothing. Every failing metric is named in one error so the
    caller fixes them in a single round trip rather than one recompile per bad name.

    ``UNRESOLVED`` takes precedence over ``AMBIGUOUS`` when both occur. An unresolved name
    is the worse failure — the caller has to find a different name, not merely disambiguate
    an existing one — and the ambiguous names ride along in the message so neither is lost.

    A metric outside ``effective_policy`` is folded into ``unresolved`` *before* it is even
    handed to the resolver (SPEC-E12 §3 stage 1), reusing the plain ``UNRESOLVED`` shape a
    nonexistent metric produces. This is a deliberate divergence from the project's usual
    bias toward maximally informative errors: a distinct forbidden code here would turn the
    error channel into an existence oracle for metric names the caller is not entitled to
    know about, which is worse than an occasionally-ambiguous "not found".
    """
    resolved: list[tuple[str, ResolverBinding]] = []
    unresolved: list[str] = []
    ambiguous: list[str] = []
    candidates: list[object] = []

    for name in query.metrics:
        if not effective_policy.metric_allowed(name):
            unresolved.append(name)
            continue
        result = resolver.resolve_metric(name, query.context)
        if isinstance(result, ResolverUnresolved):
            unresolved.append(name)
        elif isinstance(result, ResolverAmbiguous):
            ambiguous.append(name)
            candidates.extend(c for c in result.candidates if c not in candidates)
        else:
            assert isinstance(result, ResolverBinding)  # noqa: S101 — exhaustive over the union
            resolved.append((name, result))

    if unresolved:
        also = f"; also ambiguous: {_names(ambiguous)}" if ambiguous else ""
        raise Unresolved(f"metric {_names(unresolved)} matches no active binding{also}")
    if ambiguous:
        raise Ambiguous(
            f"metric {_names(ambiguous)} matches more than one active binding",
            candidates=candidates,
        )
    return resolved


def _names(names: list[str]) -> str:
    return ", ".join(repr(n) for n in names)


def compile(  # noqa: A001 — the public verb for this capability is "compile"
    query: SemanticQuery,
    resolver: ContractResolver,
    sources: list[SemanticSource],
    *,
    connection_dialects: Mapping[str, str] | None = None,
    principal: Principal | None = None,
    _dedup_leaves: bool = True,
) -> CompileResult:
    """Compile a semantic query to read-only SQL and result metadata (SPEC §4).

    ``principal`` is bound by the caller (MCP/CLI adapter) from a verified token, never from
    ``query`` itself — ``SemanticQuery`` is frozen and snapshot-locked, and accepting tenancy
    as a query field would let an agent supply its own scope (SPEC-E12, "authorization is a
    compiler input, never a query field"). ``None`` behaves as an anonymous principal with no
    tenant and no roles; against a project with no tenancy/role policy loaded this is a no-op
    and existing single-tenant behavior is unchanged.

    ``_dedup_leaves`` is private and exists for one test. Leaf deduplication is an
    optimisation, so the numbers must be identical with it disabled (AMENDMENT §3.2,
    S12 AC3) — and a dedup key that is subtly too loose is exactly the kind of bug that
    produces a plausible wrong number, so that property is worth being able to falsify
    rather than merely assert. It is not on ``SemanticQuery``: that shape is frozen, and
    this is not something a caller should ever ask for.
    """
    sources_by_name = {s.name: s for s in sources}

    # Stage 0 — bind the principal and flatten its effective policy once, before any metric
    # resolution, so a TENANT_UNRESOLVED failure never runs long enough to leak whether a
    # named metric exists (SPEC-E12 §3 stage 0).
    logger.debug("stage 0: binding principal=%s", principal)
    bound_principal = principal if principal is not None else Principal(tenant=None)
    effective_policy = resolver.authz_for(bound_principal)
    stage0_warnings: list[str] = []
    if (
        resolver.tenancy_enabled
        and not effective_policy.tenancy_exempt
        and bound_principal.tenant is None
    ):
        tenancy_policy = resolver.tenancy_policy
        assert tenancy_policy is not None  # noqa: S101 — tenancy_enabled guarantees this
        if tenancy_policy.on_missing_principal is OnMissingPrincipal.ALLOW_UNSCOPED:
            stage0_warnings.append(
                "no tenant resolved for this request; serving unscoped under "
                "on_missing_principal: allow_unscoped"
            )
        else:
            raise TenantUnresolved(
                "tenancy policy is active but the request carries no resolvable tenant"
            )

    # Stage 1 — resolve metric bindings; detect composite kinds and route accordingly.
    logger.debug("stage 1: resolving metric bindings for metrics=%s", query.metrics)
    if not query.metrics:
        raise Unresolved("query requests at least one metric")
    raw_bindings = _resolve_all_metrics(query, resolver, effective_policy)

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
    pipeline_warnings = (
        stage0_warnings
        + _enforce_min_trust(raw_bindings, resolver, query.context, trust_inputs)
        + _enforce_required_dimension(query, raw_bindings, resolver, sources_by_name)
    )

    # Stages 2-6 — plan every metric into leaves. Which kind a metric is decides the
    # shape of its leaves and nothing else: from here on a ratio, a semi-additive
    # collapse and a plain sum are the same thing to the compose step.
    dialect = _dialect_for_bindings(raw_bindings, sources_by_name, connection_dialects)
    logger.debug("stages 2-6: planning leaves for %d metric(s)", len(raw_bindings))
    planned = [
        _plan_metric(
            name,
            binding,
            query,
            resolver,
            sources_by_name,
            dialect,
            bound_principal,
            effective_policy,
        )
        for name, binding in raw_bindings
    ]

    leaves: list[LeafPlan] = []
    metric_plans: list[MetricPlan] = []
    for metric_leaves in planned:
        metric_plans.append(metric_leaves.offset(len(leaves)))
        leaves.extend(metric_leaves.leaves)

    # Stage 5b — restrict_source, for the metrics that resolve to a single source/measure.
    single_kind = [
        _bind_metric(name, b, sources_by_name)
        for name, b in raw_bindings
        if b.kind is BindingKind.SINGLE
    ]
    restrict_warnings = (
        _enforce_restrict_source(query, single_kind, resolver, None, sources_by_name)
        if single_kind
        else []
    )

    # Stages 6b-7 — fuse identical leaves, join them over a shared grain, emit one statement.
    logger.debug("stage 6b: composing %d leaves", len(leaves))
    composed = compose(leaves, metric_plans, sources_by_name, dedup=_dedup_leaves)
    adapter = adapter_for(dialect)
    sql = adapter.emit(composed.ast, limit=query.limit)

    logger.debug("stage 8: attaching result metadata")
    return CompileResult(
        sql=sql,
        dialect=adapter.dialect,
        resolved={
            name: metric_leaves.resolved
            for (name, _), metric_leaves in zip(raw_bindings, planned, strict=True)
        },
        guardrails_fired=composed.guardrails_fired,
        freshness=composed.freshness,
        warnings=[
            *pipeline_warnings,
            *restrict_warnings,
            *composed.warnings,
            *(w for m in planned for w in m.warnings),
        ],
        finality=composed.finality,
        related=related,
        trust_inputs=trust_inputs,
        composition=_first(planned, "composition"),
        partial_additive=_first(planned, "partial_additive"),
        recompute_at_grain=_first(planned, "recompute_at_grain"),
        opaque=_first(planned, "opaque"),
        scope=ScopeMetadata(
            tenant=bound_principal.tenant,
            scoped_sources=sorted(composed.scoped_sources),
            shared_sources=sorted(composed.shared_sources),
            roles=list(effective_policy.roles),
            tenancy_exempt=effective_policy.tenancy_exempt,
        ),
    )


def _plan_metric(
    name: str,
    binding: ResolverBinding,
    query: SemanticQuery,
    resolver: ContractResolver,
    sources_by_name: dict[str, SemanticSource],
    dialect: str,
    principal: Principal,
    effective_policy: EffectivePolicy,
) -> MetricLeaves:
    """Dispatch one metric to the planner for its kind (AMENDMENT §3.3)."""
    if binding.kind in {BindingKind.RATIO, BindingKind.WEIGHTED_AVG}:
        return plan_composite(
            query,
            name,
            binding,
            resolver,
            sources_by_name,
            principal=principal,
            effective_policy=effective_policy,
        )
    if binding.kind is BindingKind.SEMI_ADDITIVE:
        return plan_semi_additive(
            query,
            name,
            binding,
            resolver,
            sources_by_name,
            principal=principal,
            effective_policy=effective_policy,
        )
    if binding.kind in {BindingKind.DISTINCT_COUNT, BindingKind.PERCENTILE}:
        return plan_recompute_at_grain(
            query,
            name,
            binding,
            resolver,
            sources_by_name,
            dialect=dialect,
            principal=principal,
            effective_policy=effective_policy,
        )
    if binding.kind is BindingKind.OPAQUE:
        return plan_opaque(
            query,
            name,
            binding,
            resolver,
            sources_by_name,
            principal=principal,
            effective_policy=effective_policy,
        )
    return plan_simple_additive(
        query,
        name,
        binding,
        resolver,
        sources_by_name,
        principal=principal,
        effective_policy=effective_policy,
    )


def _first(planned: list[MetricLeaves], field: str) -> Any:
    """First requested metric's value for a per-kind metadata field, or None.

    ``CompileResult`` carries one slot per kind rather than one per metric, so a query
    mixing two ratios can only report the first one's composition. Reporting *a* correct
    composition beats reporting none; a per-metric map is a frozen-shape change and is
    deferred (AMENDMENT §10).
    """
    for metric_leaves in planned:
        value = getattr(metric_leaves, field)
        if value is not None:
            return value
    return None


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

    # A suggested dimension has to be one the caller could actually add, so it must bind
    # on *every* queried source, not merely on one of them (AMENDMENT §7). Suggesting a
    # dimension that only some metrics can be grouped by would send the caller straight
    # into an UNREACHABLE on their next request.
    seen_dims: set[str] = set()
    raw_dims: list[RelatedDimension] = []
    for src_name in sorted(queried_sources):
        for entry_name, alias in reachable_dimension_names(src_name, sources_by_name):
            if _query_references_dimension(entry_name, used_dims, filter_tokens):
                continue
            if entry_name in seen_dims:
                continue
            if not _addable_everywhere(entry_name, queried_sources, sources_by_name):
                continue
            seen_dims.add(entry_name)
            bare = entry_name.split(".")[-1]
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


def _addable_everywhere(
    entry_name: str,
    queried_sources: set[str],
    sources_by_name: dict[str, SemanticSource],
) -> bool:
    """True when *entry_name* binds from every queried source.

    Resolution is delegated to :func:`_find_dimension`, the same function the compiler
    would use when the caller actually adds the dimension, rather than re-deriving
    reachability here — a suggestion that a second implementation says is addable and the
    real one rejects is worse than no suggestion. Intersecting the raw entry names would
    not work either: qualification depends on how many aliases expose the name within one
    source's own reachable set, so the same dimension can arrive under different names
    from different leaves. A name that resolves ambiguously is not safely addable and
    drops out.
    """
    bare = entry_name.split(".")[-1]
    for src_name in queried_sources:
        tree = build_alias_tree(src_name, sources_by_name)
        with contextlib.suppress(Ambiguous):
            if _find_dimension(bare, sources_by_name, src_name, tree) is not None:
                continue
        return False
    return True
