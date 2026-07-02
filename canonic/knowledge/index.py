"""Lexical (tantivy BM25) index over committed knowledge pages (SPEC-E6 §5.1).

The lexical arm is always available — it needs no embeddings. The index covers a page's
``body`` + ``summary`` + ``tags``, with ``summary``/``tags`` boosted at query time so a
match in those concise fields outranks a body-only match (§5.2). It lives under
``.canonic/`` (local, rebuilt from committed Markdown, never committed) or in RAM for tests.

No scope/tag/usage_mode filtering happens here: the index returns ranked candidates and
:class:`~canonic.knowledge.retrieval.KnowledgeSearch` post-filters the (small) corpus,
keeping ranking and tie-breaking deterministic in one place (§10).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, NamedTuple

import tantivy

from canonic.knowledge.models import KnowledgeScope

if TYPE_CHECKING:
    from collections.abc import Iterable
    from pathlib import Path

    from canonic.knowledge.models import KnowledgePage

__all__ = [
    "KnowledgeIndex",
    "LexicalHit",
]

# Searchable text fields, in boost order. ``summary``/``tags`` are short and curated, so
# a hit there is a stronger signal than one buried in the body (§5.2).
_SEARCH_FIELDS = ("summary", "tags", "body")
_FIELD_BOOSTS = {"summary": 3.0, "tags": 2.0, "body": 1.0}


class LexicalHit(NamedTuple):
    """One ranked lexical candidate. ``rank`` is 0-based; ``score`` is BM25."""

    doc_key: str  # f"{scope}:{id}" — unique per page
    id: str
    scope: KnowledgeScope
    score: float
    rank: int


def _doc_key(page: KnowledgePage) -> str:
    """Stable unique key for a page across the index: ``"{scope}:{id}"``."""
    return f"{page.scope.value}:{page.id}"


class KnowledgeIndex:
    """A tantivy BM25 index over the knowledge-page corpus (SPEC-E6 §5.1)."""

    def __init__(self, index: tantivy.Index) -> None:
        # Construct via :meth:`build`; this holds an already-populated tantivy index.
        self._index = index

    @staticmethod
    def _schema() -> tantivy.Schema:
        builder = tantivy.SchemaBuilder()
        # Stored, for mapping a hit back to its page; not tokenized for search.
        builder.add_text_field("doc_key", stored=True)
        builder.add_text_field("id", stored=True)
        builder.add_text_field("scope", stored=True)
        # Searchable text.
        builder.add_text_field("summary", stored=False)
        builder.add_text_field("tags", stored=False)
        builder.add_text_field("body", stored=False)
        return builder.build()

    @classmethod
    def build(cls, pages: Iterable[KnowledgePage], *, path: Path | None = None) -> KnowledgeIndex:
        """(Re)build the index from ``pages``.

        ``path=None`` builds an in-RAM index (tests); a ``path`` persists the index in
        that directory under ``.canonic/`` (production). One document per page; ``tags`` is
        joined to a single whitespace-separated text field.
        """
        schema = cls._schema()
        if path is not None:
            path.mkdir(parents=True, exist_ok=True)
            index = tantivy.Index(schema, path=str(path))
        else:
            index = tantivy.Index(schema)
        writer = index.writer(heap_size=15_000_000, num_threads=1)
        # Clear any prior contents so a rebuild is a full refresh, not an append.
        writer.delete_all_documents()
        for page in pages:
            doc = tantivy.Document()
            doc.add_text("doc_key", _doc_key(page))
            doc.add_text("id", page.id)
            doc.add_text("scope", page.scope.value)
            doc.add_text("summary", page.summary)
            doc.add_text("tags", " ".join(page.tags))
            doc.add_text("body", page.body)
            writer.add_document(doc)
        writer.commit()
        index.reload()
        return cls(index)

    def search(self, query: str, *, limit: int) -> list[LexicalHit]:
        """Return up to ``limit`` BM25-ranked candidates for ``query``.

        An empty/blank query or empty corpus yields an empty list rather than raising,
        so the engine's fallback path never fails (§5.2).
        """
        if not query.strip():
            return []
        searcher = self._index.searcher()
        parsed = self._index.parse_query(
            query, default_field_names=list(_SEARCH_FIELDS), field_boosts=_FIELD_BOOSTS
        )
        result = searcher.search(parsed, limit=limit)
        scored: list[tuple[float, str, str, KnowledgeScope]] = []
        for score, address in result.hits:
            doc = searcher.doc(address)
            scored.append(
                (
                    float(score),
                    str(doc.get_first("doc_key")),
                    str(doc.get_first("id")),
                    KnowledgeScope(str(doc.get_first("scope"))),
                )
            )
        # Break BM25 score ties by the stable page id so ranks (and thus fusion) are
        # reproducible — tantivy's internal hit order is otherwise arbitrary (§10).
        scored.sort(key=lambda row: (-row[0], row[2]))
        return [
            LexicalHit(doc_key=doc_key, id=page_id, scope=scope, score=score, rank=rank)
            for rank, (score, doc_key, page_id, scope) in enumerate(scored)
        ]
