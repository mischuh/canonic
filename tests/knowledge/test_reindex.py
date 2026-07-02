"""Tests for the model-identity reindex signal on the vector store (SPEC-E10 §5, S4, GH-64).

E10 exposes an embedder fingerprint; E6 records it on the store at build time and rebuilds
when the active model changes — vectors from two models are never mixed.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from canonic.knowledge.embeddings import VectorStore

if TYPE_CHECKING:
    from collections.abc import Callable

    from canonic.knowledge.models import KnowledgePage
    from tests.knowledge.conftest import KeywordEmbedder


def _store(
    make_search_page: Callable[..., KnowledgePage], embedder: KeywordEmbedder
) -> VectorStore:
    pages = [make_search_page("p1", summary="sales", body="revenue")]
    return VectorStore.build(pages, embedder)


def test_store_records_builder_identity(
    make_search_page: Callable[..., KnowledgePage], keyword_embedder: KeywordEmbedder
) -> None:
    store = _store(make_search_page, keyword_embedder)
    assert store.model_identity == keyword_embedder.model_identity()


def test_same_model_does_not_need_reindex(
    make_search_page: Callable[..., KnowledgePage], keyword_embedder: KeywordEmbedder
) -> None:
    store = _store(make_search_page, keyword_embedder)
    assert store.needs_reindex(keyword_embedder) is False


def test_changed_model_needs_reindex(
    make_search_page: Callable[..., KnowledgePage], keyword_embedder: KeywordEmbedder
) -> None:
    from tests.knowledge.conftest import KeywordEmbedder

    store = _store(make_search_page, keyword_embedder)
    # A different model reports a different identity → the store is stale and must rebuild
    # rather than search vectors computed by another model.
    other = KeywordEmbedder([["sales"]], identity="other-model@v2")
    assert store.needs_reindex(other) is True


def test_empty_store_still_records_identity(
    make_search_page: Callable[..., KnowledgePage], keyword_embedder: KeywordEmbedder
) -> None:
    # The no-pages path still captures identity so an empty corpus can detect a model swap.
    store = VectorStore.build([], keyword_embedder)
    assert store.model_identity == keyword_embedder.model_identity()
