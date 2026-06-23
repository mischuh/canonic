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
from canon.contracts.models import BindingKind, GuardrailKind, MetricBinding, Status
from canon.exc import ContractError
from canon.semantic.loader import list_semantic_sources
from canon.semantic.models import Additivity

if TYPE_CHECKING:
    from pathlib import Path

__all__ = ["validate_contracts"]


def _validate_composite_binding(
    binding: MetricBinding,
    all_bindings: list[MetricBinding],
    source_measures: dict[str, set[str]],
) -> None:
    """Validate a ratio/weighted_avg binding (SPEC-Fuller-E15 §8, S7).

    Checks: components resolve to active metrics; no cycle in the composition graph.
    Additivity of single-kind components is verified at compile time by the safety floor.
    """
    active_by_name: dict[str, MetricBinding] = {
        b.metric: b for b in all_bindings if b.status is Status.ACTIVE
    }
    ref = binding.canonical

    if ref.kind is BindingKind.RATIO:
        component_names = [ref.numerator, ref.denominator]
        labels = ["numerator", "denominator"]
    else:  # WEIGHTED_AVG
        component_names = [ref.weighted_sum, ref.weight]
        labels = ["weighted_sum", "weight"]

    for name, label in zip(component_names, labels, strict=True):
        assert name is not None  # noqa: S101 — enforced by CanonicalRef model_validator
        if name not in active_by_name:
            raise ContractError(
                f"metric {binding.metric!r}: {label} {name!r} does not resolve "
                f"to an active metric binding"
            )

    _check_composite_cycle(binding.metric, active_by_name, path=[binding.metric])


def _check_composite_cycle(
    metric: str,
    active_by_name: dict[str, MetricBinding],
    path: list[str],
) -> None:
    """Raise ContractError if the composition graph has a cycle (SPEC §8, S7 AC2)."""
    binding = active_by_name.get(metric)
    if binding is None:
        return
    ref = binding.canonical
    if ref.kind is BindingKind.SINGLE:
        return

    if ref.kind is BindingKind.RATIO:
        children = [ref.numerator, ref.denominator]
    else:
        children = [ref.weighted_sum, ref.weight]

    for child in children:
        if child is None:
            continue
        if child in path:
            cycle_str = " → ".join(path + [child])
            raise ContractError(
                f"composite metric {path[0]!r} has a cyclic dependency: {cycle_str}"
            )
        _check_composite_cycle(child, active_by_name, path + [child])


def _validate_semi_additive_binding(
    binding: MetricBinding,
    source_measures: dict[str, set[str]],
    source_dims: dict[str, set[str]],
    source_measure_additivity: dict[str, dict[str, Additivity]],
) -> None:
    """Validate a semi_additive binding (SPEC-Fuller-E15 §8).

    Checks: source exists; base measure exists and is additive; collapse_dimension exists.
    """
    ref = binding.canonical
    assert ref.source is not None and ref.measure is not None  # noqa: S101 — enforced by model_validator
    assert ref.collapse_dimension is not None  # noqa: S101

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
    additivity = source_measure_additivity.get(ref.source, {}).get(ref.measure)
    if additivity is not Additivity.ADDITIVE:
        raise ContractError(
            f"metric {binding.metric!r}: base measure {ref.source}.{ref.measure!r} "
            f"must be additive for a semi_additive binding (got {additivity!r})"
        )
    if ref.collapse_dimension not in source_dims.get(ref.source, set()):
        raise ContractError(
            f"metric {binding.metric!r}: collapse_dimension {ref.collapse_dimension!r} "
            f"is not declared as a dimension on source {ref.source!r}"
        )


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
    source_dims: dict[str, set[str]] = {s.name: {d.name for d in s.dimensions} for s in sources}
    source_measure_additivity: dict[str, dict[str, Additivity]] = {
        s.name: {m.name: m.additivity for m in s.measures} for s in sources
    }
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
        if ref.kind is BindingKind.SINGLE:
            assert ref.source is not None and ref.measure is not None  # noqa: S101
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
        elif ref.kind is BindingKind.SEMI_ADDITIVE:
            _validate_semi_additive_binding(
                binding, source_measures, source_dims, source_measure_additivity
            )
        else:
            _validate_composite_binding(binding, bindings, source_measures)

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
