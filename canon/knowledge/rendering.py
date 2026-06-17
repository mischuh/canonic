"""Live-definition rendering for knowledge pages (SPEC-E6 §7).

A page never copies a measure's SQL. It references the measure by name and a
``{{ sl:<entity>.expr }}`` directive renders the **live** ``expr`` at read time, so the
rendered definition can never fall out of sync with the semantic layer (S6 AC1).

Rendering is read-side and **drift-tolerant**: a directive that does not resolve to a live
measure is left verbatim and never raises, mirroring graph traversal's "drift must never
crash a read-side walk". Write-time validation (:mod:`canon.knowledge.validation`) already
blocks pages whose ``sl_refs`` point at nothing, so an unresolved directive at read time is
an edge case (e.g. an entity that disappeared after the page was committed).
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from canon.knowledge.models import KnowledgePage
    from canon.knowledge.validation import EntityIndex

__all__ = [
    "DefinitionRenderer",
]

# Captures the fully-qualified entity name from ``{{ sl:<entity>.expr }}``. Only ``.expr`` is
# supported in v1 — the sole attribute in the spec; other forms are left untouched.
_DIRECTIVE_RE = re.compile(r"\{\{\s*sl:([A-Za-z0-9_.]+)\.expr\s*\}\}")


class DefinitionRenderer:
    """Resolves ``{{ sl:<entity>.expr }}`` directives against a live entity index (§7).

    The index is injected once (mirroring :class:`~canon.knowledge.validation.ReferenceValidator`);
    each render reads the current measure ``expr`` so a changed definition re-renders
    automatically without editing the page.
    """

    def __init__(self, entity_index: EntityIndex) -> None:
        self._entities = entity_index

    def render_body(self, body: str) -> str:
        """Return ``body`` with every resolvable ``.expr`` directive replaced by live SQL.

        An unresolved directive (unknown entity, or a name that is not a measure) is left
        verbatim — rendering never raises on drift.
        """

        def _replace(match: re.Match[str]) -> str:
            measure = self._entities.measures.get(match.group(1))
            return measure.expr if measure is not None else match.group(0)

        return _DIRECTIVE_RE.sub(_replace, body)

    def render(self, page: KnowledgePage) -> str:
        """Render ``page.body`` with live measure definitions substituted in."""
        return self.render_body(page.body)
