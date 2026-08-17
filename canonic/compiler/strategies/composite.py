"""Composite compile path (composable_post_agg: ratio / weighted_avg, SPEC §4.1)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from canonic.compiler.compose import Combine, LeafRef, MetricLeaves, MetricPlan
from canonic.compiler.leaf import LeafContext, LeafMetric, LeafPlan, plan_leaf
from canonic.compiler.result import CompositionMetadata
from canonic.contracts.models import BindingKind, OnZeroDenominator
from canonic.exc import Unresolved, UnsupportedMeasure

if TYPE_CHECKING:
    from canonic.compiler.query import SemanticQuery
    from canonic.contracts.principal import EffectivePolicy, Principal
    from canonic.contracts.resolver import Binding as ResolverBinding
    from canonic.contracts.resolver import ComponentBindings, ContractResolver
    from canonic.semantic.models import SemanticSource

from canonic.compiler._helpers import _find_measure, _ResolvedMetric


def _plan_leaf(
    component: ResolverBinding,
    query: SemanticQuery,
    resolver: ContractResolver,
    sources_by_name: dict[str, SemanticSource],
    principal: Principal,
    effective_policy: EffectivePolicy,
    population_filter: str | None = None,
) -> LeafPlan:
    """Bind one single-kind component to its measure, then plan it as a leaf.

    Stages 2-6 live in :func:`canonic.compiler.leaf.plan_leaf`, shared with every other
    compile path. What stays here is what is specific to being a *component*: rejecting a
    nested composite, and resolving the component's source and measure so the error names
    the component rather than a metric the caller never asked for.
    """
    if component.kind is not BindingKind.SINGLE:
        raise UnsupportedMeasure(
            f"nested composite metrics are not yet supported; "
            f"component {component.metric!r} has kind {component.kind!r}"
        )
    assert component.source is not None and component.measure is not None  # noqa: S101

    source_name = component.source
    source_obj = sources_by_name.get(source_name)
    if source_obj is None:
        raise Unresolved(f"component {component.metric!r} binds to unknown source {source_name!r}")
    measure_obj = _find_measure(source_obj, component.measure)
    if measure_obj is None:
        raise Unresolved(
            f"component {component.metric!r} binds to unknown measure "
            f"{source_name}.{component.measure!r}"
        )

    return plan_leaf(
        LeafContext(
            query=query,
            resolver=resolver,
            sources_by_name=sources_by_name,
            principal=principal,
            effective_policy=effective_policy,
        ),
        source_name,
        [
            LeafMetric(
                resolved=_ResolvedMetric(
                    name=component.metric, source=source_name, measure=measure_obj
                ),
                population_filter=population_filter,
            )
        ],
        finality_metric=component.metric,
    )


#: How ``on_zero_denominator`` maps onto a compose expression, and whether the caller is
#: warned. Only ``null`` warns: it is the default, and a NULL where a number was expected
#: is worth saying out loud. ``zero`` and ``error`` were both asked for explicitly.
_ZERO_POLICY: dict[OnZeroDenominator, Combine] = {
    OnZeroDenominator.NULL: Combine.RATIO_NULL,
    OnZeroDenominator.ZERO: Combine.RATIO_ZERO,
    OnZeroDenominator.ERROR: Combine.RATIO_RAW,
}


def _combine_population_filters(*filters: str | None) -> str | None:
    """AND together a composite-level and a component-level population_filter (§4.5).

    Both the ratio metric itself and each of its numerator/denominator building blocks
    may declare a population_filter; a leaf must honor whichever of its own owner's
    filter and the composite's filter are present, not just one of the two.
    """
    present = [f for f in filters if f]
    if not present:
        return None
    return " AND ".join(f"({f})" for f in present)


def plan_metric(
    query: SemanticQuery,
    queried_name: str,
    composite: ResolverBinding,
    resolver: ContractResolver,
    sources_by_name: dict[str, SemanticSource],
    *,
    principal: Principal,
    effective_policy: EffectivePolicy,
) -> MetricLeaves:
    """Plan a composable_post_agg metric: a leaf per component, divided after aggregation.

    The unifying rule is aggregate first, combine last. Each component is planned as an
    independent leaf at the requested grain so its own guardrails and safety floor fire on
    its own rows (SPEC §4.1, §6, AC3), and the division happens once, in the outer SELECT,
    over values that are already correct at that grain.

    Since the amendment this is not a parallel mechanism but the general compose step with
    two leaves: if both components share a plan they fuse into a single CTE, and a
    component that is also requested standalone is emitted once and referenced twice.
    """
    assert composite.components is not None  # noqa: S101 — routing guarantees composite kind
    components: ComponentBindings = composite.components
    on_zero = components.on_zero_denominator

    # Both the composite metric and each component may declare a population, and a leaf
    # has to honour both of its own, not just one (§4.5).
    composite_pop_filter = composite.binding.canonical.population_filter
    leaves = [
        _plan_leaf(
            component,
            query,
            resolver,
            sources_by_name,
            principal,
            effective_policy,
            _combine_population_filters(
                composite_pop_filter, component.binding.canonical.population_filter
            ),
        )
        for component in (components.numerator, components.denominator)
    ]

    num_name = components.numerator.metric
    den_name = components.denominator.metric
    resolved_key = composite.resolved_key
    assert resolved_key is not None  # noqa: S101 — ratio/weighted_avg always resolve a key
    zero_warnings: tuple[str, ...] = ()
    if on_zero is OnZeroDenominator.NULL:
        zero_warnings = (
            f"zero denominator for metric {composite.metric!r} yields NULL "
            f"(on_zero_denominator=null)",
        )

    return MetricLeaves(
        leaves=leaves,
        metric=MetricPlan(
            name=composite.metric,
            refs=tuple(
                LeafRef(leaf=i, column=leaf.measure_aliases[0]) for i, leaf in enumerate(leaves)
            ),
            combine=_ZERO_POLICY[on_zero],
        ),
        resolved=resolved_key,
        composition=CompositionMetadata(
            kind=composite.kind,
            numerator=num_name,
            denominator=den_name,
            on_zero_denominator=on_zero,
        ),
        warnings=zero_warnings,
    )
