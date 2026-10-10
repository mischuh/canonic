"""Composite compile path (composable_post_agg: ratio / weighted_avg, SPEC §4.1)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from canonic.compiler.compose import Combine, LeafRef, MetricLeaves, MetricPlan
from canonic.compiler.leaf import LeafContext, LeafMetric, LeafPlan, plan_leaf
from canonic.compiler.result import CompositionMetadata
from canonic.compiler.strategies.recompute import plan_metric as plan_recompute_at_grain
from canonic.contracts.models import BindingKind, OnZeroDenominator
from canonic.exc import Unresolved, UnsupportedMeasure

if TYPE_CHECKING:
    from canonic.compiler.query import SemanticQuery
    from canonic.contracts.principal import EffectivePolicy, Principal
    from canonic.contracts.resolver import Binding as ResolverBinding
    from canonic.contracts.resolver import ComponentBindings, ContractResolver
    from canonic.semantic.models import SemanticSource

from canonic.compiler._helpers import _combine_population_filters, _find_measure, _ResolvedMetric

#: Composite kinds, the only components that are genuinely nested rather than unsupported leaves.
_COMPOSITE_KINDS = frozenset({BindingKind.RATIO, BindingKind.WEIGHTED_AVG})


def _plan_leaf(
    component: ResolverBinding,
    query: SemanticQuery,
    resolver: ContractResolver,
    sources_by_name: dict[str, SemanticSource],
    principal: Principal,
    effective_policy: EffectivePolicy,
    dialect: str,
    composite_population_filter: str | None = None,
) -> LeafPlan:
    """Plan one component of a composite as a leaf, by the strategy for its own kind.

    Stages 2-6 live in :func:`canonic.compiler.leaf.plan_leaf`, shared with every other
    compile path. What stays here is what is specific to being a *component*: which kinds
    may serve as one (``single`` and ``distinct_count``), and resolving the component's
    source and measure so an error names the component rather than a metric the caller
    never asked for.
    """
    if component.kind is BindingKind.DISTINCT_COUNT:
        # A distinct count must go through its own planner. The additive default would
        # compile it as a sum, which is a plausible and wrong number.
        return plan_recompute_at_grain(
            query,
            component.metric,
            component,
            resolver,
            sources_by_name,
            dialect=dialect,
            principal=principal,
            effective_policy=effective_policy,
            parent_population_filter=composite_population_filter,
        ).leaves[0]
    if component.kind is not BindingKind.SINGLE:
        nested = (
            "nested composite metrics are not yet supported; "
            if (component.kind in _COMPOSITE_KINDS)
            else ""
        )
        raise UnsupportedMeasure(
            f"{nested}component {component.metric!r} has kind {str(component.kind)!r}, "
            f"but only single and distinct_count metrics can be ratio components"
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
                population_filter=_combine_population_filters(
                    composite_population_filter, component.binding.canonical.population_filter
                ),
                alias=component.metric,
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


def plan_metric(
    query: SemanticQuery,
    queried_name: str,
    composite: ResolverBinding,
    resolver: ContractResolver,
    sources_by_name: dict[str, SemanticSource],
    *,
    dialect: str = "postgres",
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
            dialect,
            composite_pop_filter,
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
