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

from canon.contracts.loader import load_finality, load_guardrails, load_metric_bindings
from canon.contracts.models import (
    CanonicalRef,
    FinalityRule,
    Guardrail,
    GuardrailKind,
    MetricBinding,
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
    "ContractResolver",
    "MetricResolution",
    "Unresolved",
]


@dataclass(frozen=True, slots=True)
class Binding:
    """A metric name resolved to exactly one canonical source+measure.

    ``source``/``measure`` mirror ``binding.canonical`` for convenience.
    """

    metric: str
    source: str
    measure: str
    binding: MetricBinding


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
    ) -> None:
        self._guardrails: list[Guardrail] = list(guardrails)
        self._finality_by_metric: dict[str, FinalityRule] = {r.metric: r for r in finality}

        # name/alias -> active bindings; multiple entries for a name means ambiguity
        name_index: dict[str, list[MetricBinding]] = {}
        # active metric name -> its canonical (source, measure), for metric-targeted guardrails
        metric_to_canonical: dict[str, CanonicalRef] = {}
        for binding in bindings:
            if binding.status is not Status.ACTIVE:
                continue
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
        )

    def resolve_metric(self, name: str, context: str | None = None) -> MetricResolution:
        """Resolve a metric name/alias to its canonical binding (SPEC-E5-E15 §6).

        Zero matches → :class:`Unresolved`; exactly one → :class:`Binding`;
        more than one → :class:`Ambiguous` with all candidates. Matching is exact;
        ``context`` is accepted for interface stability but does not affect metric
        resolution in P0.
        """
        matches = self._name_index.get(name, [])
        if not matches:
            return Unresolved(name=name)
        if len(matches) == 1:
            binding = matches[0]
            return Binding(
                metric=binding.metric,
                source=binding.canonical.source,
                measure=binding.canonical.measure,
                binding=binding,
            )
        candidates = tuple(sorted(matches, key=lambda b: (b.metric, b.aliases)))
        return Ambiguous(name=name, candidates=candidates)

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

    def assertions_for(self, query: dict[str, Any]) -> list[Assertion]:
        """Assertions relevant to a query — always ``[]`` in P0 (SPEC-E5-E15 §2.5 is P1)."""
        return []
