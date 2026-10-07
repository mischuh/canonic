"""Declarations the compiler accepts but does not act on (CHANGELOG-spec-drift §C15).

These fields are parsed and validated, so a typo is caught, but nothing in the compiler reads
them. A reader of the YAML would reasonably assume otherwise, so ``canonic validate`` and
``canonic mcp start`` report each one instead of leaving the no-op silent:

- named ``filters`` on a semantic source: the compiler never resolves them,
- ``segments``: reserved, not implemented,
- a finality rule's ``coalescing`` that is not the canonical window expression: source
  selection follows the watermark alone, so any other expression cannot be honored,
- ``board_only_final: true`` without a ``restrict_source`` guardrail for the metric: final-only
  is enforced by that guardrail, the flag itself does nothing.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from canonic.contracts.loader import load_finality, load_guardrails
from canonic.contracts.models import GuardrailKind
from canonic.semantic.loader import list_semantic_sources

if TYPE_CHECKING:
    from pathlib import Path

    from canonic.contracts.models import FinalityRule, Guardrail

__all__ = ["inert_declaration_warnings"]

#: The one coalescing expression the watermark logic implements.
_CANONICAL_COALESCING = "window <= watermark ? final : provisional"


def inert_declaration_warnings(project_root: Path) -> list[str]:
    """One human-readable warning per declaration that has no effect, in a stable order."""
    warnings: list[str] = []
    for source in list_semantic_sources(project_root):
        if source.filters:
            names = ", ".join(f.name for f in source.filters)
            warnings.append(
                f"source {source.name!r}: named filters ({names}) are not applied by the "
                "compiler, put the predicate in a measure, a population_filter or a guardrail"
            )
        if source.segments:
            warnings.append(f"source {source.name!r}: segments are reserved and have no effect")

    guardrails = load_guardrails(project_root)
    for rule in load_finality(project_root):
        warnings.extend(_finality_warnings(rule, guardrails))
    return warnings


def _finality_warnings(rule: FinalityRule, guardrails: list[Guardrail]) -> list[str]:
    found: list[str] = []
    if rule.coalescing is not None and _normalize(rule.coalescing) != _CANONICAL_COALESCING:
        found.append(
            f"finality rule for {rule.metric!r}: coalescing {rule.coalescing!r} is not "
            "evaluated, the source is chosen by the final realization's watermark alone"
        )
    if rule.board_only_final and not _has_restrict_source(rule, guardrails):
        found.append(
            f"finality rule for {rule.metric!r}: board_only_final has no effect on its own "
            "and no restrict_source guardrail covers this metric, so provisional rows can "
            "still be served in every context"
        )
    return found


def _normalize(expression: str) -> str:
    return " ".join(expression.split())


def _has_restrict_source(rule: FinalityRule, guardrails: list[Guardrail]) -> bool:
    realization_sources = {r.source for r in rule.realizations}
    return any(
        g.kind is GuardrailKind.RESTRICT_SOURCE
        and (g.applies_to.metric == rule.metric or g.applies_to.source in realization_sources)
        for g in guardrails
    )
