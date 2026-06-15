"""Cross-surface validation for the contracts layer (SPEC-E15 §7)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from canon.contracts.loader import load_guardrails, load_metric_bindings
from canon.contracts.models import Status
from canon.exc import ContractError
from canon.semantic.loader import list_semantic_sources

if TYPE_CHECKING:
    from pathlib import Path

__all__ = ["validate_contracts"]


def validate_contracts(project_root: Path) -> None:
    """Validate all contracts against the live semantic sources.

    Raises ContractError on the first cross-surface violation:
    - Active binding's canonical.source/measure does not exist in semantics/.
    - Guardrail applies_to.source (or .measure) does not exist in semantics/.
    - Guardrail applies_to.metric does not resolve to an active binding.
    """
    sources = list_semantic_sources(project_root)
    source_measures: dict[str, set[str]] = {s.name: {m.name for m in s.measures} for s in sources}

    bindings = load_metric_bindings(project_root)
    active_metrics = {b.metric for b in bindings if b.status is Status.ACTIVE}

    for binding in bindings:
        if binding.status is not Status.ACTIVE:
            continue
        ref = binding.canonical
        if ref.source not in source_measures:
            raise ContractError(
                f"metric {binding.metric!r}: canonical.source {ref.source!r} "
                f"does not match any semantic source"
            )
        if ref.measure not in source_measures[ref.source]:
            raise ContractError(
                f"metric {binding.metric!r}: canonical.measure {ref.measure!r} "
                f"is not declared on source {ref.source!r}"
            )

    guardrails = load_guardrails(project_root)
    for guardrail in guardrails:
        at = guardrail.applies_to
        if at.source is not None:
            if at.source not in source_measures:
                raise ContractError(
                    f"guardrail {guardrail.id!r}: applies_to.source {at.source!r} "
                    f"does not match any semantic source"
                )
            if at.measure is not None and at.measure not in source_measures[at.source]:
                raise ContractError(
                    f"guardrail {guardrail.id!r}: applies_to.measure {at.measure!r} "
                    f"is not declared on source {at.source!r}"
                )
        elif at.metric is not None:
            if at.metric not in active_metrics:
                raise ContractError(
                    f"guardrail {guardrail.id!r}: applies_to.metric {at.metric!r} "
                    f"does not resolve to an active metric binding"
                )
