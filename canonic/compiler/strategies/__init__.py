"""Per-kind leaf planning.

Each module turns one requested metric into the leaves that serve it plus the expression
that assembles them, as a :class:`~canonic.compiler.compose.MetricLeaves`. What differs
between kinds is the shape of a leaf and what counts as a corrupting join; how leaves are
deduplicated, named, joined and merged is the same for all of them and lives in
:mod:`canonic.compiler.compose`.
"""

from canonic.compiler.strategies.composite import plan_metric as plan_composite
from canonic.compiler.strategies.opaque import plan_metric as plan_opaque
from canonic.compiler.strategies.recompute import plan_metric as plan_recompute_at_grain
from canonic.compiler.strategies.semi_additive import plan_metric as plan_semi_additive
from canonic.compiler.strategies.simple_additive import plan_metric as plan_simple_additive

__all__ = [
    "plan_composite",
    "plan_opaque",
    "plan_recompute_at_grain",
    "plan_semi_additive",
    "plan_simple_additive",
]
