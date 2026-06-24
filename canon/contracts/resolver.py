"""The contract↔compiler seam — the single authority on canonicality (SPEC-E5-E15 §6).

The compiler (E5) calls a :class:`ContractResolver` and trusts the result; no
canonicality logic lives in the compiler. ``resolve_metric`` returns a result object
(:class:`Binding` / :class:`Ambiguous` / :class:`Unresolved`) rather than raising —
the compiler decides whether to map an :class:`Ambiguous`/:class:`Unresolved` result
onto the same-named exception in :mod:`canon.exc` (which carries the headless error
code). These result types and those exceptions are intentionally distinct: import
both explicitly.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from canon.contracts.loader import (
    load_assertions,
    load_finality,
    load_guardrails,
    load_metric_bindings,
)
from canon.contracts.models import (
    BindingKind,
    CanonicalRef,
    CollapseAgg,
    FinalityRule,
    Guardrail,
    GuardrailKind,
    MetricBinding,
    OnZeroDenominator,
    Status,
)

if TYPE_CHECKING:
    from collections.abc import Iterable
    from pathlib import Path
    from typing import Any

    from canon.contracts.models import Assertion

__all__ = [
    "Ambiguous",
    "Binding",
    "ComponentBindings",
    "ContractResolver",
    "MetricResolution",
    "OpaqueBinding",
    "RecomputeAtGrainBinding",
    "SemiAdditiveBinding",
    "Unresolved",
]


@dataclass(frozen=True, slots=True)
class SemiAdditiveBinding:
    """Resolved semi_additive parameters for a partial_additive metric (§4.2)."""

    collapse_dimension: str
    collapse_agg: CollapseAgg


@dataclass(frozen=True, slots=True)
class RecomputeAtGrainBinding:
    """Resolved recompute_at_grain parameters for a distinct_count or percentile metric (§4.3)."""

    kind: BindingKind
    distinct_on: str | None
    column: str | None
    quantile: float | None


@dataclass(frozen=True, slots=True)
class OpaqueBinding:
    """Resolved opaque parameters for a grain-locked metric (§4.4)."""

    native_grain: list[str]


@dataclass(frozen=True, slots=True)
class Binding:
    """A metric name resolved to its canonical definition.

    For ``kind=single``, ``source`` and ``measure`` are non-None.
    For composite kinds (ratio/weighted_avg), ``source`` and ``measure`` are None;
    ``components`` carries the resolved numerator/denominator bindings.
    For ``kind=semi_additive``, ``source`` and ``measure`` are non-None (single-leaf);
    ``semi_additive`` carries the collapse parameters.
    """

    metric: str
    source: str | None
    measure: str | None
    binding: MetricBinding
    kind: BindingKind = BindingKind.SINGLE
    components: ComponentBindings | None = None
    semi_additive: SemiAdditiveBinding | None = None
    recompute_at_grain: RecomputeAtGrainBinding | None = None
    opaque: OpaqueBinding | None = None


@dataclass(frozen=True, slots=True)
class ComponentBindings:
    """Resolved component bindings for a composable_post_agg metric (§4.1).

    ``weighted_avg`` maps weighted_sum→numerator, weight→denominator so the
    compile path is identical to ratio.
    """

    numerator: Binding
    denominator: Binding
    on_zero_denominator: OnZeroDenominator


@dataclass(frozen=True, slots=True)
class Ambiguous:
    """A metric name that matched more than one active binding.

    ``candidates`` is stable-sorted so identical inputs yield identical results.
    Distinct from :class:`canon.exc.Ambiguous` (the exception with an error code).
    """

    name: str
    candidates: tuple[MetricBinding, ...]


@dataclass(frozen=True, slots=True)
class Unresolved:
    """A metric name that matched no active binding.

    Distinct from :class:`canon.exc.Unresolved` (the exception with an error code).
    """

    name: str


MetricResolution = Binding | Ambiguous | Unresolved


class ContractResolver:
    """The only authority on canonicality, queried by the compiler at hook points.

    Instantiated once per project load (see :meth:`from_project`) and injected into
    the compiler. All indices are built once at construction so repeated queries are
    deterministic and cheap (SPEC-E5-E15 §6).
    """

    def __init__(
        self,
        bindings: Iterable[MetricBinding],
        guardrails: Iterable[Guardrail],
        finality: Iterable[FinalityRule] = (),
        assertions: Iterable[Assertion] = (),
    ) -> None:
        self._guardrails: list[Guardrail] = list(guardrails)
        self._finality_by_metric: dict[str, FinalityRule] = {r.metric: r for r in finality}
        self._assertions: list[Assertion] = list(assertions)

        # name/alias -> active bindings; multiple entries for a name means ambiguity
        name_index: dict[str, list[MetricBinding]] = {}
        # active single/semi_additive metric name -> canonical for metric-targeted guardrails
        # composite bindings (ratio/weighted_avg) have no single (source, measure), so excluded
        metric_to_canonical: dict[str, CanonicalRef] = {}
        for binding in bindings:
            if binding.status is not Status.ACTIVE:
                continue
            if binding.canonical.kind in {
                BindingKind.SINGLE,
                BindingKind.SEMI_ADDITIVE,
                BindingKind.DISTINCT_COUNT,
                BindingKind.PERCENTILE,
                BindingKind.OPAQUE,
            }:
                metric_to_canonical[binding.metric] = binding.canonical
            for name in (binding.metric, *binding.aliases):
                name_index.setdefault(name, []).append(binding)
        self._name_index = name_index
        self._metric_to_canonical = metric_to_canonical

    @classmethod
    def from_project(cls, project_root: Path) -> ContractResolver:
        """Load bindings, guardrails, and finality rules from a project root."""
        return cls(
            bindings=load_metric_bindings(project_root),
            guardrails=load_guardrails(project_root),
            finality=load_finality(project_root),
            assertions=load_assertions(project_root),
        )

    def resolve_metric(self, name: str, context: str | None = None) -> MetricResolution:
        """Resolve a metric name/alias to its canonical binding (SPEC-E5-E15 §6, §4.1).

        Zero matches → :class:`Unresolved`; exactly one → :class:`Binding`;
        more than one → :class:`Ambiguous`. For composite kinds (ratio/weighted_avg),
        components are resolved recursively; a cycle returns :class:`Unresolved`.
        ``context`` is accepted for interface stability but does not affect resolution in P0.
        """
        matches = self._name_index.get(name, [])
        if not matches:
            return Unresolved(name=name)
        if len(matches) > 1:
            candidates = tuple(sorted(matches, key=lambda b: (b.metric, b.aliases)))
            return Ambiguous(name=name, candidates=candidates)
        return self._resolve_binding(matches[0], seen=frozenset({name}))

    def _resolve_component(
        self, name: str, seen: frozenset[str]
    ) -> Binding | Ambiguous | Unresolved:
        if name in seen:
            return Unresolved(name=name)
        matches = self._name_index.get(name, [])
        if not matches:
            return Unresolved(name=name)
        if len(matches) > 1:
            candidates = tuple(sorted(matches, key=lambda b: (b.metric, b.aliases)))
            return Ambiguous(name=name, candidates=candidates)
        return self._resolve_binding(matches[0], seen | {name})

    def _resolve_binding(
        self, binding: MetricBinding, seen: frozenset[str]
    ) -> Binding | Ambiguous | Unresolved:
        canonical = binding.canonical
        if canonical.kind is BindingKind.SINGLE:
            return Binding(
                metric=binding.metric,
                source=canonical.source,
                measure=canonical.measure,
                binding=binding,
            )

        if canonical.kind is BindingKind.SEMI_ADDITIVE:
            assert (  # noqa: S101 — enforced by model_validator
                canonical.source is not None
                and canonical.measure is not None
                and canonical.collapse_dimension is not None
                and canonical.collapse_agg is not None
            )
            return Binding(
                metric=binding.metric,
                source=canonical.source,
                measure=canonical.measure,
                binding=binding,
                kind=BindingKind.SEMI_ADDITIVE,
                semi_additive=SemiAdditiveBinding(
                    collapse_dimension=canonical.collapse_dimension,
                    collapse_agg=canonical.collapse_agg,
                ),
            )

        if canonical.kind in {BindingKind.DISTINCT_COUNT, BindingKind.PERCENTILE}:
            assert canonical.source is not None  # noqa: S101 — enforced by model_validator
            return Binding(
                metric=binding.metric,
                source=canonical.source,
                measure=None,
                binding=binding,
                kind=canonical.kind,
                recompute_at_grain=RecomputeAtGrainBinding(
                    kind=canonical.kind,
                    distinct_on=canonical.distinct_on,
                    column=canonical.column,
                    quantile=canonical.quantile,
                ),
            )

        if canonical.kind is BindingKind.OPAQUE:
            assert (  # noqa: S101 — enforced by model_validator
                canonical.source is not None
                and canonical.measure is not None
                and canonical.native_grain is not None
            )
            return Binding(
                metric=binding.metric,
                source=canonical.source,
                measure=canonical.measure,
                binding=binding,
                kind=BindingKind.OPAQUE,
                opaque=OpaqueBinding(native_grain=list(canonical.native_grain)),
            )

        if canonical.kind is BindingKind.RATIO:
            num_name, den_name = canonical.numerator, canonical.denominator
        else:  # WEIGHTED_AVG: weighted_sum → numerator, weight → denominator
            num_name, den_name = canonical.weighted_sum, canonical.weight

        assert num_name is not None and den_name is not None  # noqa: S101 — enforced by model_validator

        num_result = self._resolve_component(num_name, seen)
        if not isinstance(num_result, Binding):
            return num_result

        den_result = self._resolve_component(den_name, seen)
        if not isinstance(den_result, Binding):
            return den_result

        return Binding(
            metric=binding.metric,
            source=None,
            measure=None,
            binding=binding,
            kind=canonical.kind,
            components=ComponentBindings(
                numerator=num_result,
                denominator=den_result,
                on_zero_denominator=canonical.on_zero_denominator,
            ),
        )

    def guardrails_for(
        self,
        source: str,
        measure: str,
        ctx: str | None = None,
    ) -> list[Guardrail]:
        """Return guardrails applying to ``(source, measure)``, stable-sorted by ``id``.

        Matches guardrails targeting the source/measure directly, plus metric-targeted
        guardrails whose metric resolves to this exact ``(source, measure)``. ``ctx`` is
        reserved for context-scoped kinds (``restrict_source``, P1) and has no filtering
        effect in P0.
        """
        matched = [g for g in self._guardrails if self._guardrail_applies(g, source, measure)]
        return sorted(matched, key=lambda g: g.id)

    def _guardrail_applies(self, guardrail: Guardrail, source: str, measure: str) -> bool:
        at = guardrail.applies_to
        if at.source is not None:
            # measure=None is source-wide; otherwise both must match
            return at.source == source and (at.measure is None or at.measure == measure)
        if at.metric is not None:
            ref = self._metric_to_canonical.get(at.metric)
            return ref is not None and ref.source == source and ref.measure == measure
        return False

    def restrict_source_for(
        self,
        source: str,
        measure: str,
        context: str | None,
    ) -> list[Guardrail]:
        """Return restrict_source guardrails active for this (source, measure) and context.

        Only returns guardrails when ``context`` is not None and matches ``g.context``.
        Stable-sorted by ``id``.
        """
        if context is None:
            return []
        matched = [
            g
            for g in self._guardrails
            if g.kind is GuardrailKind.RESTRICT_SOURCE
            and g.context == context
            and self._guardrail_applies(g, source, measure)
        ]
        return sorted(matched, key=lambda g: g.id)

    def finality_for(self, metric: str) -> FinalityRule | None:
        """Return the finality rule for a metric, or ``None`` if no rule is defined."""
        return self._finality_by_metric.get(metric)

    def all_assertions(self) -> list[Assertion]:
        """Every loaded assertion, in load order (E16's accuracy harness consumes these)."""
        return list(self._assertions)

    def assertions_for(self, query: dict[str, Any]) -> list[Assertion]:
        """Executable assertions whose semantic query requests the same metric set.

        An assertion is *relevant* to a query when both request exactly the same set of
        metrics — so the user's ad-hoc query triggers the trusted checks written for those
        metrics (informationally in normal mode, as a gate under ``--harness``). Candidate
        assertions still in raw ``{native, references}`` form (not yet executable) are
        excluded (SPEC-Fuller-E15 §3.2).
        """
        from canon.contracts.assertions import assertion_metrics, is_executable

        wanted = set(query.get("metrics", []))
        if not wanted:
            return []
        return [
            a for a in self._assertions if is_executable(a) and set(assertion_metrics(a)) == wanted
        ]
