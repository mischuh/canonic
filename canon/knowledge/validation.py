"""Write-time reference validation for knowledge pages (SPEC-E6 §3.1).

Every page write — by an E4 draft or a human edit — must resolve its references before
the page can be indexed. A page carries three reference kinds:

- ``sl_refs`` → semantic entities (E5/E15), checked against an :class:`EntityIndex`;
- ``refs`` → other pages, checked against a :class:`PageIndex` within a visible scope;
- ``[[wikilinks]]`` in the body, parsed and checked the same way as ``refs``.

A broken reference raises :class:`~canon.exc.KnowledgeReferenceError`, blocking the write
with a precise location. The first broken reference wins.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict

from canon.exc import KnowledgeReferenceError
from canon.knowledge.models import KnowledgeScope

if TYPE_CHECKING:
    from collections.abc import Iterable

    from canon.knowledge.models import KnowledgePage
    from canon.semantic.models import SemanticSource

__all__ = [
    "EntityIndex",
    "PageIndex",
    "ReferenceValidator",
]

# Captures the slug from ``[[slug]]`` / ``[[slug|alias]]``; ignores ``{{ sl:… }}`` tags.
_WIKILINK_RE = re.compile(r"\[\[\s*([^\]|]+?)\s*(?:\|[^\]]*)?\]\]")


class EntityIndex(BaseModel):
    """The set of live, fully-qualified semantic-entity names ``sl_refs`` resolve against.

    A name is ``{connection}.{source}`` for a source, or ``{connection}.{source}.{member}``
    for each column, measure, and dimension. E5/E15 own the authoritative index; this is the
    minimal membership view E6 validates against.
    """

    model_config = ConfigDict(frozen=True)

    names: frozenset[str]

    def __contains__(self, fq_name: str) -> bool:
        return fq_name in self.names

    @classmethod
    def from_sources(cls, sources: Iterable[SemanticSource]) -> EntityIndex:
        """Enumerate every fully-qualified entity name exposed by ``sources``."""
        names: set[str] = set()
        for source in sources:
            base = f"{source.connection}.{source.name}"
            names.add(base)
            for column in source.columns:
                names.add(f"{base}.{column.name}")
            for measure in source.measures:
                names.add(f"{base}.{measure.name}")
            for dimension in source.dimensions:
                names.add(f"{base}.{dimension.name}")
        return cls(names=frozenset(names))


class PageIndex(BaseModel):
    """Known page slugs grouped by scope, with the SPEC-E6 §4 visibility rule.

    A GLOBAL page may reference only GLOBAL pages; a USER page may reference GLOBAL and USER
    pages (USER is strictly additive over GLOBAL).
    """

    model_config = ConfigDict(frozen=True)

    slugs_by_scope: dict[KnowledgeScope, frozenset[str]]

    @classmethod
    def from_pages(cls, pages: Iterable[KnowledgePage]) -> PageIndex:
        """Group page slugs by their scope."""
        grouped: dict[KnowledgeScope, set[str]] = {}
        for page in pages:
            grouped.setdefault(page.scope, set()).add(page.id)
        return cls(slugs_by_scope={scope: frozenset(s) for scope, s in grouped.items()})

    def is_visible(self, slug: str, from_scope: KnowledgeScope) -> bool:
        """True if ``slug`` names a page visible from a page in ``from_scope``."""
        if slug in self.slugs_by_scope.get(KnowledgeScope.GLOBAL, frozenset()):
            return True
        if from_scope is KnowledgeScope.USER:
            return slug in self.slugs_by_scope.get(KnowledgeScope.USER, frozenset())
        return False


class ReferenceValidator:
    """Validates a page's references against an entity index and a page index (§3.1).

    Dependencies are injected once; each ``validate_*`` method raises
    :class:`~canon.exc.KnowledgeReferenceError` on the first broken reference it finds.
    """

    def __init__(self, entity_index: EntityIndex, page_index: PageIndex) -> None:
        self._entities = entity_index
        self._pages = page_index

    def validate_sl_refs(self, page: KnowledgePage) -> None:
        """Each ``sl_ref`` must resolve to a live semantic entity."""
        for i, ref in enumerate(page.sl_refs):
            if ref not in self._entities:
                raise KnowledgeReferenceError(
                    ("sl_refs", i),
                    ref,
                    "sl_ref",
                    f"{page.path}: sl_ref {ref!r} resolves to no live semantic entity",
                )

    def validate_refs(self, page: KnowledgePage) -> None:
        """Each page ``ref`` must point at an existing page in a visible scope."""
        for i, ref in enumerate(page.refs):
            if not self._pages.is_visible(ref, page.scope):
                raise KnowledgeReferenceError(
                    ("refs", i),
                    ref,
                    "ref",
                    f"{page.path}: ref {ref!r} points at no page visible from {page.scope.value} scope",
                )

    def validate_wikilinks(self, page: KnowledgePage) -> None:
        """Each ``[[link]]`` in the body must point at a page in a visible scope."""
        for match in _WIKILINK_RE.finditer(page.body):
            slug = match.group(1).strip()
            if not self._pages.is_visible(slug, page.scope):
                raise KnowledgeReferenceError(
                    ("body",),
                    slug,
                    "wikilink",
                    f"{page.path}: [[{slug}]] points at no page visible from {page.scope.value} scope",
                )

    def validate_page(self, page: KnowledgePage) -> None:
        """Run all three checks (sl_refs → refs → wikilinks); first error wins.

        Returns ``None`` only when every reference resolves; a broken page is never indexed.
        """
        self.validate_sl_refs(page)
        self.validate_refs(page)
        self.validate_wikilinks(page)
