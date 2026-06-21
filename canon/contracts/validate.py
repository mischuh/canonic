"""Cross-surface validation for the contracts layer (SPEC-E15 §7)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from canon.contracts.assertions import assertion_metrics, is_executable
from canon.contracts.finality import validate_finality_rule
from canon.contracts.loader import (
    load_assertions,
    load_finality,
    load_guardrails,
    load_metric_bindings,
)
from canon.contracts.models import GuardrailKind, Status
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
    - Guardrail applies_to.metric does not resolve to an active metric binding.
    - Finality rule's metric does not resolve to an active binding (§5.1).
    - Finality rule's realization sources do not exist in semantics/ (§5.1).
    - Assertion's query metrics do not resolve, or its expected values name a column
      that is not one of the query's output columns (metric/dimension) (§5.2).
    """
    sources = list_semantic_sources(project_root)
    source_measures: dict[str, set[str]] = {s.name: {m.name for m in s.measures} for s in sources}
    source_names = set(source_measures)

    bindings = load_metric_bindings(project_root)
    active_metrics = {b.metric for b in bindings if b.status is Status.ACTIVE}
    active_names = {
        n for b in bindings if b.status is Status.ACTIVE for n in (b.metric, *b.aliases)
    }

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

    finality_metrics = {rule.metric for rule in load_finality(project_root)}

    for guardrail in guardrails:
        if guardrail.kind is not GuardrailKind.RESTRICT_SOURCE:
            continue
        at = guardrail.applies_to
        if at.metric is not None and at.metric not in finality_metrics:
            raise ContractError(
                f"guardrail {guardrail.id!r}: restrict_source guardrail targets metric "
                f"{at.metric!r} which has no finality rule — watermark cannot be evaluated"
            )

    finality_rules = load_finality(project_root)
    for rule in finality_rules:
        if rule.metric not in active_metrics:
            raise ContractError(
                f"finality rule for metric {rule.metric!r} does not resolve to an active binding"
            )
        try:
            validate_finality_rule(rule, source_names=source_names)
        except ValueError as exc:
            raise ContractError(f"finality rule for metric {rule.metric!r}: {exc}") from exc

    for assertion in load_assertions(project_root):
        # Candidate assertions still in raw {native, references} form (E3 ingestion) are
        # not yet executable semantic queries — they are validated when a human completes them.
        if not is_executable(assertion):
            continue
        metrics = assertion_metrics(assertion)
        for metric in metrics:
            if metric not in active_names:
                raise ContractError(
                    f"assertion {assertion.id!r}: query metric {metric!r} does not resolve "
                    f"to an active metric binding"
                )
        dimensions = assertion.query.get("dimensions", [])
        output_columns = set(metrics) | (set(dimensions) if isinstance(dimensions, list) else set())
        for col in assertion.expect.values:
            if col not in output_columns:
                raise ContractError(
                    f"assertion {assertion.id!r}: expected value {col!r} is not an output column "
                    f"of the query (metrics: {sorted(metrics)}, dimensions: {sorted(dimensions)})"
                )
        # A query with no dimensions returns a single scalar row; expecting more is a shape error.
        if not dimensions and assertion.expect.rows is not None and assertion.expect.rows > 1:
            raise ContractError(
                f"assertion {assertion.id!r}: query has no dimensions so it returns one row, "
                f"but expect.rows is {assertion.expect.rows}"
            )
