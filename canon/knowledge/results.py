"""Retrieval result models — the ``SearchResult`` shape (SPEC-E6 §5.3).

A P1 capability surface, **additive** to the frozen P0 serving contract: it introduces
``search``/``search_context`` output without touching ``query``/``compile``/errors
(SPEC P0-interface-freeze §4.1). Models are frozen, matching the rest of the knowledge
layer.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict

from canon.knowledge.models import (  # noqa: TC001 — Pydantic resolves annotations at runtime
    KnowledgePage,
    KnowledgeScope,
)

__all__ = [
    "Annotation",
    "Hit",
    "MatchedOn",
    "SearchResult",
    "Subgraph",
]


class MatchedOn(StrEnum):
    """Which retrieval arm surfaced a hit (SPEC-E6 §5.3)."""

    LEXICAL = "lexical"  # tantivy BM25 arm — always available
    VECTOR = "vector"  # numpy cosine arm — only when embeddings are installed


class Annotation(BaseModel):
    """A strict-additive user page attached to a global hit (SPEC-E6 §4).

    The global page stays authoritative; this carries the colliding user page as a
    personal annotation, never a replacement.
    """

    model_config = ConfigDict(frozen=True)

    page: str  # the annotating user page's id/slug
    scope: str  # owner-qualified scope label, e.g. "user:alice"


class Hit(BaseModel):
    """One ranked page in a search result (SPEC-E6 §5.3)."""

    model_config = ConfigDict(frozen=True)

    page: str  # page id/slug
    scope: KnowledgeScope
    score: float  # fused (RRF) score; higher is better
    summary: str
    matched_on: list[MatchedOn]  # arm(s) that surfaced this hit
    sl_refs: list[str] = []  # bound semantic entities (E5)
    annotations: list[Annotation] = []  # attached user pages (§4 strict-additive)


class Subgraph(BaseModel):
    """The connected context bundle returned by graph traversal (SPEC-E6 §6).

    ``expand`` walks the reference graph from seed hits and returns the deduped, bounded
    set of pages reached plus the live semantic entities they bind. ``pages`` is what flows
    into :attr:`SearchResult.traversed`; ``entities`` is carried for callers that want the
    reached ``sl_ref`` targets directly.
    """

    model_config = ConfigDict(frozen=True)

    pages: list[KnowledgePage] = []
    entities: list[str] = []  # sl_ref targets reached, sorted


class SearchResult(BaseModel):
    """The full result of a hybrid search (SPEC-E6 §5.3)."""

    model_config = ConfigDict(frozen=True)

    hits: list[Hit]
    # Graph-expanded pages (§6); set to a traversal's ``Subgraph.pages``. Empty unless the
    # caller requested expansion — additive to the §5.3 shape.
    traversed: list[KnowledgePage] = []
