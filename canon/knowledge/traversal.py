"""Graph traversal over committed knowledge pages (SPEC-E6 §6).

After search returns seed hits, an agent pulls connected context by **traversing the
reference graph without re-searching** (PRD FR-5). :class:`GraphTraversal` walks from each
seed breadth-first, following ``sl_refs`` / ``refs`` / ``[[links]]`` up to a bounded depth,
dedupes, and returns the connected :class:`~canon.knowledge.results.Subgraph` — the pages
reached plus the live semantic entities they bind. A bounded depth and a node cap keep it
from pulling the whole graph; the walk is deterministic given the committed files (§10).

This module imports nothing from the retrieval/embeddings layer: traversal operates purely
over the loaded pages, which structurally guarantees the "no second search call" property.

.. note::
   SPEC-E6 §6 sketches ``expand(seeds, page_index, entity_index, …)``. ``PageIndex`` stores
   only slugs grouped by scope and cannot materialize a :class:`KnowledgePage`, which a walk
   must do to follow a ref. So the corpus is supplied to ``__init__`` (mirroring
   :class:`~canon.knowledge.retrieval.KnowledgeSearch`) and ``entity_index`` becomes the
   optional dependency that filters reached ``sl_refs`` to live entities.
"""

from __future__ import annotations

from collections import deque
from typing import TYPE_CHECKING

from canon.knowledge.models import KnowledgePage, KnowledgeScope
from canon.knowledge.results import Subgraph
from canon.knowledge.validation import iter_wikilink_slugs

if TYPE_CHECKING:
    from collections.abc import Iterable

    from canon.knowledge.results import Hit
    from canon.knowledge.validation import EntityIndex

__all__ = [
    "GraphTraversal",
]

#: Defaults that keep an expansion small enough to stay a single coherent bundle (§6).
_DEFAULT_MAX_DEPTH = 2
_DEFAULT_MAX_NODES = 50


def _doc_key(scope: KnowledgeScope, slug: str) -> str:
    """Scope-qualified identity for a page; the dedup key shared with retrieval."""
    return f"{scope.value}:{slug}"


class GraphTraversal:
    """Breadth-first expansion of seed hits over the page reference graph (SPEC-E6 §6)."""

    def __init__(
        self,
        pages: Iterable[KnowledgePage],
        *,
        entity_index: EntityIndex | None = None,
    ) -> None:
        """Build the slug→page lookups the walk resolves against.

        ``pages`` is the already scope-filtered corpus (visibility is the caller's job, as in
        :meth:`KnowledgeSearch.search`). ``entity_index``, when supplied, filters the reached
        ``sl_refs`` down to live semantic entities; without it every reached ``sl_ref`` is
        reported.
        """
        page_list = list(pages)
        self._by_doc_key: dict[str, KnowledgePage] = {_doc_key(p.scope, p.id): p for p in page_list}
        # A ref/wikilink names a bare slug; resolve GLOBAL-authoritative on a collision so a
        # user page never shadows the global it annotates (§4 strict-additive).
        self._by_slug: dict[str, KnowledgePage] = {}
        for page in page_list:
            existing = self._by_slug.get(page.id)
            if existing is None or page.scope is KnowledgeScope.GLOBAL:
                self._by_slug[page.id] = page
        self._entity_index = entity_index

    def expand(
        self,
        seeds: Iterable[Hit],
        *,
        max_depth: int = _DEFAULT_MAX_DEPTH,
        max_nodes: int = _DEFAULT_MAX_NODES,
    ) -> Subgraph:
        """Expand ``seeds`` into one deduped, bounded subgraph (SPEC-E6 §6, S3 AC1).

        From each seed, follow ``refs`` / ``[[links]]`` breadth-first up to ``max_depth``
        edges, admitting at most ``max_nodes`` pages. The same page is never admitted twice
        (dedup + cycle safety), and ordering is deterministic: seeds and each node's neighbors
        are visited in sorted order, and the result preserves admission order.
        """
        # Insertion-ordered: doubles as dedup set, cycle guard, and stable result order.
        admitted: dict[str, KnowledgePage] = {}
        queue: deque[tuple[str, int]] = deque()

        seed_pages = (self._by_doc_key.get(_doc_key(s.scope, s.page)) for s in seeds)
        for page in sorted(
            (p for p in seed_pages if p is not None),
            key=lambda p: (p.scope.value, p.id),
        ):
            self._admit(page, 0, admitted, queue, max_nodes)

        while queue:
            doc_key, depth = queue.popleft()
            if depth >= max_depth:
                continue
            page = admitted[doc_key]
            for neighbor in self._neighbors(page):
                self._admit(neighbor, depth + 1, admitted, queue, max_nodes)

        entities = self._collect_entities(admitted.values())
        return Subgraph(pages=list(admitted.values()), entities=entities)

    def _admit(
        self,
        page: KnowledgePage,
        depth: int,
        admitted: dict[str, KnowledgePage],
        queue: deque[tuple[str, int]],
        max_nodes: int,
    ) -> None:
        """Admit ``page`` if unseen and the node cap has room; enqueue it for expansion."""
        if len(admitted) >= max_nodes:
            return
        key = _doc_key(page.scope, page.id)
        if key in admitted:
            return
        admitted[key] = page
        queue.append((key, depth))

    def _neighbors(self, page: KnowledgePage) -> list[KnowledgePage]:
        """The page-valued neighbors of ``page``: sorted, deduped ``refs`` ∪ ``[[links]]``.

        Unknown slugs are skipped — references are validated at write time, but post-prune
        drift must never crash a read-side walk.
        """
        slugs = set(page.refs) | set(iter_wikilink_slugs(page.body))
        return [
            neighbor for slug in sorted(slugs) if (neighbor := self._by_slug.get(slug)) is not None
        ]

    def _collect_entities(self, pages: Iterable[KnowledgePage]) -> list[str]:
        """Sorted, deduped ``sl_refs`` of the admitted pages, kept to live entities if known."""
        names = {ref for page in pages for ref in page.sl_refs}
        if self._entity_index is not None:
            names = {name for name in names if name in self._entity_index}
        return sorted(names)
