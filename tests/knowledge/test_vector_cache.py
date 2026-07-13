"""Tests for the persistent vector-embedding cache (SPEC-E6 §5.1, §5.3).

``VectorIndexCache`` exists so a search only re-embeds pages whose content changed since
the last call, instead of re-running the embedding model over the whole knowledge base on
every search (see ``canonic/knowledge/vector_cache.py`` module docstring).
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from canonic.knowledge.embeddings import VectorStore
from canonic.knowledge.vector_cache import VectorIndexCache

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence
    from pathlib import Path

    import numpy as np

    from canonic.knowledge.models import KnowledgePage
    from tests.knowledge.conftest import KeywordEmbedder


class _CountingEmbedder:
    """Wraps an embedder and records every ``embed()`` call, without touching the shared fixture."""

    def __init__(self, inner: KeywordEmbedder) -> None:
        self._inner = inner
        self.calls: list[list[str]] = []

    def embed(self, texts: Sequence[str]) -> np.ndarray:
        self.calls.append(list(texts))
        return self._inner.embed(texts)

    def model_identity(self) -> str:
        return self._inner.model_identity()


def test_first_call_embeds_and_persists_all_pages(
    tmp_path: Path,
    make_search_page: Callable[..., KnowledgePage],
    keyword_embedder: KeywordEmbedder,
) -> None:
    pages = [
        make_search_page("p1", summary="revenue", body="sales figures"),
        make_search_page("p2", summary="customer", body="client notes"),
    ]
    embedder = _CountingEmbedder(keyword_embedder)
    cache_path = tmp_path / "vectors.json"

    store = VectorIndexCache(cache_path).load_or_build(pages, embedder)

    assert len(embedder.calls) == 1
    assert set(embedder.calls[0]) == {"revenue\nsales figures", "customer\nclient notes"}
    assert isinstance(store, VectorStore)
    assert cache_path.exists()


def test_unchanged_pages_are_not_re_embedded(
    tmp_path: Path,
    make_search_page: Callable[..., KnowledgePage],
    keyword_embedder: KeywordEmbedder,
) -> None:
    pages = [make_search_page("p1", summary="revenue", body="sales")]
    cache_path = tmp_path / "vectors.json"
    VectorIndexCache(cache_path).load_or_build(pages, _CountingEmbedder(keyword_embedder))

    second = _CountingEmbedder(keyword_embedder)
    store = VectorIndexCache(cache_path).load_or_build(pages, second)

    assert second.calls == []
    # The reused vector is still genuinely usable for search, not just structurally present.
    hits = store.search("revenue", keyword_embedder, limit=5)
    assert [h.id for h in hits] == ["p1"]


def test_edited_page_only_re_embeds_that_page(
    tmp_path: Path,
    make_search_page: Callable[..., KnowledgePage],
    keyword_embedder: KeywordEmbedder,
) -> None:
    cache_path = tmp_path / "vectors.json"
    p1 = make_search_page("p1", summary="revenue", body="sales")
    p2 = make_search_page("p2", summary="customer", body="client")
    VectorIndexCache(cache_path).load_or_build([p1, p2], _CountingEmbedder(keyword_embedder))

    p1_edited = make_search_page("p1", summary="revenue", body="sales figures updated")
    second = _CountingEmbedder(keyword_embedder)
    VectorIndexCache(cache_path).load_or_build([p1_edited, p2], second)

    assert second.calls == [["revenue\nsales figures updated"]]


def test_removed_page_is_dropped_from_cache(
    tmp_path: Path,
    make_search_page: Callable[..., KnowledgePage],
    keyword_embedder: KeywordEmbedder,
) -> None:
    cache_path = tmp_path / "vectors.json"
    p1 = make_search_page("p1", summary="revenue", body="sales")
    p2 = make_search_page("p2", summary="customer", body="client")
    VectorIndexCache(cache_path).load_or_build([p1, p2], keyword_embedder)

    VectorIndexCache(cache_path).load_or_build([p1], keyword_embedder)

    persisted = json.loads(cache_path.read_text())
    assert [e["id"] for e in persisted["entries"]] == ["p1"]


def test_model_identity_change_triggers_full_rebuild(
    tmp_path: Path,
    make_search_page: Callable[..., KnowledgePage],
    keyword_embedder: KeywordEmbedder,
) -> None:
    from tests.knowledge.conftest import KeywordEmbedder as _KE

    cache_path = tmp_path / "vectors.json"
    pages = [make_search_page("p1", summary="revenue", body="sales")]
    VectorIndexCache(cache_path).load_or_build(pages, keyword_embedder)

    other = _CountingEmbedder(_KE([["sales"]], identity="other-model@v2"))
    store = VectorIndexCache(cache_path).load_or_build(pages, other)

    assert len(other.calls) == 1  # not skipped despite an existing cache entry for p1
    assert store.model_identity == "other-model@v2"


def test_corrupt_cache_file_rebuilds_cleanly(
    tmp_path: Path,
    make_search_page: Callable[..., KnowledgePage],
    keyword_embedder: KeywordEmbedder,
) -> None:
    cache_path = tmp_path / "vectors.json"
    cache_path.write_text("{not valid json")

    pages = [make_search_page("p1", summary="revenue", body="sales")]
    store = VectorIndexCache(cache_path).load_or_build(pages, keyword_embedder)

    assert isinstance(store, VectorStore)
    persisted = json.loads(cache_path.read_text())
    assert persisted["model_identity"] == keyword_embedder.model_identity()


def test_empty_pages_produce_empty_store_without_error(
    tmp_path: Path,
    keyword_embedder: KeywordEmbedder,
) -> None:
    cache_path = tmp_path / "vectors.json"
    store = VectorIndexCache(cache_path).load_or_build([], keyword_embedder)
    assert store.search("revenue", keyword_embedder, limit=5) == []
