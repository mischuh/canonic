"""Static trust inputs for a resolved metric, including the components of composites."""

from __future__ import annotations

from typing import TYPE_CHECKING

from canonic.compiler.result import TrustInput

if TYPE_CHECKING:
    from canonic.contracts.resolver import Binding, ContractResolver

__all__ = ["trust_inputs_for_binding"]


def trust_inputs_for_binding(
    name: str, binding: Binding, resolver: ContractResolver
) -> list[TrustInput]:
    """One :class:`TrustInput` for the metric itself, then one per component, recursively.

    A ratio or weighted average is computed from its numerator and denominator, so it is only
    as trustworthy as they are. The scorer is worst-signal-dominates, so emitting the
    component inputs next to the composite's own is the conservative merge: the composite's
    tier is the worst tier across its own signals and those of every component, nested
    composites included. Each component input carries ``owner`` (the queried composite) so
    the resulting reasons name the component that caused the cap.
    """
    inputs = [_input(name, binding, resolver, owner=None)]
    inputs.extend(_component_inputs(name, binding, resolver))
    return inputs


def _input(
    name: str, binding: Binding, resolver: ContractResolver, owner: str | None
) -> TrustInput:
    return TrustInput(
        metric=name,
        provenance=binding.binding.provenance.value,
        has_assertion=bool(resolver.assertions_for({"metrics": [name]})),
        binding=binding.resolved_key,
        owner=owner,
    )


def _component_inputs(owner: str, binding: Binding, resolver: ContractResolver) -> list[TrustInput]:
    if binding.components is None:
        return []
    inputs: list[TrustInput] = []
    for component in (binding.components.numerator, binding.components.denominator):
        inputs.append(_input(component.metric, component, resolver, owner=owner))
        inputs.extend(_component_inputs(owner, component, resolver))
    return inputs
