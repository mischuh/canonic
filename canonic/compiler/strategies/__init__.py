"""Per-binding-kind compile strategies for the deterministic compiler (SPEC §4)."""

from __future__ import annotations

from canonic.compiler.strategies.composite import _compile_composite
from canonic.compiler.strategies.opaque import _compile_opaque
from canonic.compiler.strategies.recompute import _compile_recompute_at_grain
from canonic.compiler.strategies.semi_additive import _compile_semi_additive
from canonic.compiler.strategies.simple_additive import _compile_simple_additive

__all__ = [
    "_compile_composite",
    "_compile_opaque",
    "_compile_recompute_at_grain",
    "_compile_semi_additive",
    "_compile_simple_additive",
]
