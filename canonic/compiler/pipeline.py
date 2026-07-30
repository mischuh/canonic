"""The deterministic compiler pipeline (SPEC-E5-E15 §4, stages 1–4, 6–8).

``compile`` turns a :class:`SemanticQuery` into dialect-correct, read-only SQL plus
result metadata. No LLM, no wall-clock, no randomness: identical inputs yield
byte-identical SQL (SPEC §8). The :class:`ContractResolver` is the only authority on
canonicality — the compiler trusts its results and never reimplements them (§6)."""

from __future__ import annotations

import dataclasses
import logging
from typing import TYPE_CHECKING

from canonic.compiler._helpers import _block_or_warn, _find_dimension, _query_references_dimension
from canonic.compiler.joins import build_alias_tree, reachable_dimension_names
from canonic.compiler.result import (
    CompileResult,
    RelatedDimension,
    RelatedMetadata,
    RelatedMetric,
    TrustInput,
)
from canonic.contracts.models import BindingKind
from canonic.contracts.resolver import Ambiguous as ResolverAmbiguous
from canonic.contracts.resolver import Binding as ResolverBinding
from canonic.contracts.resolver import Unresolved as ResolverUnresolved
from canonic.exc import Ambiguous, Unresolved, UnsupportedMeasure
from canonic.trust.models import TrustTier, tier_meets
from canonic.trust.scorer import TrustScorer
from canonic.trust.signals import static_signals_for

if TYPE_CHECKING:
    from collections.abc import Mapping

    from canonic.compiler.query import SemanticQuery
    from canonic.contracts.resolver import ContractResolver
    from canonic.semantic.models import SemanticSource

from canonic.compiler.strategies import (
    _compile_composite,
    _compile_opaque,
    _compile_recompute_at_grain,
    _compile_semi_additive,
    _compile_simple_additive,
)

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
        binding_str = (
            f"{binding.source}.{binding.measure}"
            if binding.source is not None and binding.measure is not None
            else None
        )
        inputs.append(
            TrustInput(
                metric=name,
                provenance=binding.binding.provenance.value,
                has_assertion=has_assertion,
                binding=binding_str,
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
    pipeline_warnings = _enforce_min_trust(
        raw_bindings, resolver, query.context, trust_inputs
    ) + _enforce_required_dimension(query, raw_bindings, resolver, sources_by_name)

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
        composite_result = _compile_composite(
            query, composite, resolver, sources_by_name, dialect=dialect
        )
        return dataclasses.replace(
            composite_result,
            related=related,
            trust_inputs=trust_inputs,
            warnings=pipeline_warnings + composite_result.warnings,
        )
    if semi_additive_indices:
        if len(query.metrics) > 1:
            raise UnsupportedMeasure(
                "semi_additive metrics must be queried alone; "
                "remove other metrics from the request or split into separate queries"
            )
        _, sa_binding = raw_bindings[0]
        logger.info("compile path: semi_additive metric=%s", query.metrics[0])
        semi_additive_result = _compile_semi_additive(
            query, sa_binding, resolver, sources_by_name, dialect=dialect
        )
        return dataclasses.replace(
            semi_additive_result,
            related=related,
            trust_inputs=trust_inputs,
            warnings=pipeline_warnings + semi_additive_result.warnings,
        )
    if recompute_indices:
        if len(query.metrics) > 1:
            raise UnsupportedMeasure(
                "recompute_at_grain metrics (distinct_count/percentile) must be queried alone; "
                "remove other metrics from the request or split into separate queries"
            )
        _, rg_binding = raw_bindings[0]
        logger.info("compile path: recompute_at_grain metric=%s", query.metrics[0])
        recompute_result = _compile_recompute_at_grain(
            query, rg_binding, resolver, sources_by_name, dialect=dialect
        )
        return dataclasses.replace(
            recompute_result,
            related=related,
            trust_inputs=trust_inputs,
            warnings=pipeline_warnings + recompute_result.warnings,
        )
    if opaque_indices:
        if len(query.metrics) > 1:
            raise UnsupportedMeasure(
                "opaque metrics must be queried alone; "
                "remove other metrics from the request or split into separate queries"
            )
        _, opaque_binding = raw_bindings[0]
        logger.info("compile path: opaque metric=%s", query.metrics[0])
        opaque_result = _compile_opaque(
            query, opaque_binding, resolver, sources_by_name, dialect=dialect
        )
        return dataclasses.replace(
            opaque_result,
            related=related,
            trust_inputs=trust_inputs,
            warnings=pipeline_warnings + opaque_result.warnings,
        )

    logger.info("compile path: simple/additive metrics=%s", query.metrics)
    simple_additive_result = _compile_simple_additive(
        query, raw_bindings, resolver, sources_by_name, dialect=dialect
    )
    return dataclasses.replace(
        simple_additive_result,
        related=related,
        trust_inputs=trust_inputs,
        warnings=pipeline_warnings + simple_additive_result.warnings,
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
            if _query_references_dimension(entry_name, used_dims, filter_tokens):
                continue
            bare = entry_name.split(".")[-1]
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
