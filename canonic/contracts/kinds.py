"""Per-kind strategy registry for :class:`BindingKind` (SPEC-Fuller-E15 §3).

Historically every consumer that needed something kind-specific about a metric binding —
which field carries its column, whether it is composite, which sub-metrics it composes —
re-derived it with its own ``if kind is …`` chain. Adding a new :class:`BindingKind` meant
editing a handful of unrelated modules (service, resolver, validate, evidence, …).

This module is the single place that knowledge lives: one :class:`BindingKindSpec` per kind,
registered in a lookup table, mirroring :class:`canonic.connectors.factory.ConnectorFactory`.
Consumers ask the registry (or the derived category sets) instead of branching, so a new kind
is declared here and nowhere else.

It intentionally depends only on :mod:`canonic.contracts.models`; it must never import the
resolver, compiler, or validators, so any of them can depend on it without an import cycle.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from canonic.contracts.models import BindingKind

if TYPE_CHECKING:
    from canonic.contracts.models import CanonicalRef

__all__ = [
    "COMPOSITE_KINDS",
    "DESCRIBABLE_KINDS",
    "RECOMPUTE_KINDS",
    "SOURCE_BOUND_KINDS",
    "BindingKindSpec",
    "register",
    "spec_for",
]


@dataclass(frozen=True, slots=True)
class BindingKindSpec:
    """Everything a consumer needs to know about one kind without branching on it.

    ``column_attr`` names the :class:`~canonic.contracts.models.CanonicalRef` attribute that
    holds the physical column a source-bound kind reads (``measure`` / ``distinct_on`` /
    ``column``); ``None`` for composite kinds. ``component_attrs`` names the two attributes
    holding the sub-metric references for composite kinds (e.g. ``("numerator", "denominator")``);
    ``None`` for source-bound kinds. The attribute names double as the human-facing labels used
    in validation error messages.
    """

    kind: BindingKind
    is_source_bound: bool  # reads exactly one physical (source, column) pair
    is_composite: bool  # composes other metrics (ratio / weighted_avg)
    is_recompute: bool  # recomputed at grain (distinct_count / percentile)
    is_describable: bool  # surfaced as a source metric by describe_metric / list_metrics
    column_attr: str | None
    component_attrs: tuple[str, str] | None

    def column_field(self, ref: CanonicalRef) -> str | None:
        """The column/measure this kind reads on its source, or ``None`` for composite kinds."""
        if self.column_attr is None:
            return None
        value = getattr(ref, self.column_attr)
        return None if value is None else str(value)

    def component_names(self, ref: CanonicalRef) -> tuple[str | None, str | None]:
        """The two component metric names for composite kinds, else ``(None, None)``."""
        if self.component_attrs is None:
            return (None, None)
        first, second = self.component_attrs
        return (getattr(ref, first), getattr(ref, second))


_REGISTRY: dict[BindingKind, BindingKindSpec] = {}


def register(spec: BindingKindSpec) -> None:
    """Register the spec for a kind. Mirrors ``ConnectorFactory.register``: no silent overwrite."""
    if spec.kind in _REGISTRY:
        raise ValueError(f"BindingKind {spec.kind!r} already registered")
    _REGISTRY[spec.kind] = spec


def spec_for(kind: BindingKind) -> BindingKindSpec:
    """Return the spec for *kind*.

    Raises :class:`ValueError` if a kind has no spec — the registry is expected to cover every
    :class:`BindingKind` member (enforced by ``tests/contracts/test_kinds.py``), so a miss is a
    programming error, not a runtime condition.
    """
    try:
        return _REGISTRY[kind]
    except KeyError:
        raise ValueError(f"no BindingKindSpec registered for {kind!r}") from None


register(
    BindingKindSpec(
        BindingKind.SINGLE,
        is_source_bound=True,
        is_composite=False,
        is_recompute=False,
        is_describable=True,
        column_attr="measure",
        component_attrs=None,
    )
)
register(
    BindingKindSpec(
        BindingKind.SEMI_ADDITIVE,
        is_source_bound=True,
        is_composite=False,
        is_recompute=False,
        is_describable=True,
        column_attr="measure",
        component_attrs=None,
    )
)
register(
    BindingKindSpec(
        BindingKind.DISTINCT_COUNT,
        is_source_bound=True,
        is_composite=False,
        is_recompute=True,
        is_describable=True,
        column_attr="distinct_on",
        component_attrs=None,
    )
)
register(
    BindingKindSpec(
        BindingKind.PERCENTILE,
        is_source_bound=True,
        is_composite=False,
        is_recompute=True,
        is_describable=True,
        column_attr="column",
        component_attrs=None,
    )
)
register(
    BindingKindSpec(
        BindingKind.OPAQUE,
        is_source_bound=True,
        is_composite=False,
        is_recompute=False,
        is_describable=False,  # grain-locked: use query(), not describe_metric()
        column_attr="measure",
        component_attrs=None,
    )
)
register(
    BindingKindSpec(
        BindingKind.RATIO,
        is_source_bound=False,
        is_composite=True,
        is_recompute=False,
        is_describable=False,
        column_attr=None,
        component_attrs=("numerator", "denominator"),
    )
)
register(
    BindingKindSpec(
        BindingKind.WEIGHTED_AVG,
        is_source_bound=False,
        is_composite=True,
        is_recompute=False,
        is_describable=False,
        column_attr=None,
        component_attrs=("weighted_sum", "weight"),
    )
)


# Category sets derived from the registry so a newly registered kind joins them automatically.
SOURCE_BOUND_KINDS: frozenset[BindingKind] = frozenset(
    k for k, s in _REGISTRY.items() if s.is_source_bound
)
COMPOSITE_KINDS: frozenset[BindingKind] = frozenset(
    k for k, s in _REGISTRY.items() if s.is_composite
)
RECOMPUTE_KINDS: frozenset[BindingKind] = frozenset(
    k for k, s in _REGISTRY.items() if s.is_recompute
)
DESCRIBABLE_KINDS: frozenset[BindingKind] = frozenset(
    k for k, s in _REGISTRY.items() if s.is_describable
)
