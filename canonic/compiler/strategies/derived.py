"""Derived compile path: arithmetic over other metrics, applied after each is aggregated.

Every input is planned as if it were requested alone, by its own kind, at the requested
grain. Its leaves join the query's spine like those of any other metric, and an input that is
also requested on its own is computed once. The arithmetic happens in the final SELECT, over
each input's finished value: a ratio already divided, a running total already accumulated.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import sqlglot
from sqlglot import exp

from canonic.compiler._helpers import enclosing_population
from canonic.compiler.compose import (
    Combine,
    LeafRef,
    MetricLeaves,
    MetricPlan,
    guard_division,
    operand_placeholder,
    zero_denominator_warning,
)
from canonic.compiler.result import DerivedInput, DerivedMetadata
from canonic.contracts.models import OnZeroDenominator

if TYPE_CHECKING:
    from collections.abc import Callable

    from canonic.compiler.leaf import LeafPlan
    from canonic.contracts.resolver import Binding as ResolverBinding

__all__ = ["plan_metric"]

_ZERO_POLICY: dict[OnZeroDenominator, Combine] = {
    OnZeroDenominator.NULL: Combine.RATIO_NULL,
    OnZeroDenominator.ZERO: Combine.RATIO_ZERO,
    OnZeroDenominator.ERROR: Combine.RATIO_RAW,
}


def plan_metric(
    queried_name: str,
    binding: ResolverBinding,
    *,
    plan_input: Callable[[str, ResolverBinding], MetricLeaves],
) -> MetricLeaves:
    """Plan every input, and the expression that combines their values."""
    assert binding.derived is not None  # noqa: S101 — routing guarantees derived kind
    derived = binding.derived
    leaves: list[LeafPlan] = []
    operands: list[MetricPlan] = []
    planned_inputs: list[MetricLeaves] = []
    for _alias, input_binding in derived.inputs:
        with enclosing_population(binding.binding.canonical.population_filter):
            planned = plan_input(input_binding.metric, input_binding)
        operands.append(planned.offset(len(leaves)))
        leaves.extend(planned.leaves)
        planned_inputs.append(planned)

    combine = _ZERO_POLICY[derived.on_zero_denominator]
    template = _template(derived.expr, [name for name, _ in derived.inputs], combine)
    divides = template.find(exp.Div) is not None
    warnings = tuple(w for p in planned_inputs for w in p.warnings)
    if divides and combine is Combine.RATIO_NULL:
        warnings = (*warnings, zero_denominator_warning(queried_name))

    return MetricLeaves(
        leaves=leaves,
        metric=MetricPlan(
            name=queried_name,
            refs=tuple(LeafRef(leaf=r.leaf, column=r.column) for op in operands for r in op.refs),
            combine=Combine.DERIVED,
            operands=tuple(operands),
            template=template,
        ),
        resolved=binding.resolved_key or queried_name,
        offsets=tuple(o for p in planned_inputs for o in p.offsets),
        derived=(
            DerivedMetadata(
                metric=queried_name,
                expr=derived.expr,
                inputs=[
                    DerivedInput(alias=name, metric=b.metric, resolved=b.resolved_key)
                    for name, b in derived.inputs
                ],
                on_zero_denominator=str(derived.on_zero_denominator),
            ),
            *(d for p in planned_inputs for d in p.derived),
        ),
        warnings=warnings,
    )


def _template(expr: str, names: list[str], combine: Combine) -> exp.Expression:
    """``expr`` with input ``i`` written as ``_op<i>`` and every division guarded."""
    placeholders = {name: operand_placeholder(i) for i, name in enumerate(names)}

    def rename(node: exp.Expression) -> exp.Expression:
        if isinstance(node, exp.Column) and not node.table and node.name in placeholders:
            return exp.column(placeholders[node.name])
        return node

    parsed = sqlglot.parse_one(expr, read="postgres").transform(rename)
    return _guarded(parsed, combine)


def _guarded(node: exp.Expression, combine: Combine) -> exp.Expression:
    """``node`` with every division, nested ones included, under the zero-denominator policy."""
    if isinstance(node, exp.Div):
        return guard_division(
            _guarded(node.this, combine), _guarded(node.expression, combine), combine
        )
    rebuilt = node.copy()
    for key, value in list(rebuilt.args.items()):
        if isinstance(value, exp.Expression):
            rebuilt.set(key, _guarded(value, combine))
        elif isinstance(value, list):
            rebuilt.set(
                key, [_guarded(v, combine) if isinstance(v, exp.Expression) else v for v in value]
            )
    return rebuilt
