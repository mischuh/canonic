"""Unit tests for the lexical (tantivy BM25) index (SPEC-E6 §5.1)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from canonic.knowledge.index import KnowledgeIndex
from canonic.knowledge.models import KnowledgeScope

if TYPE_CHECKING:
    from collections.abc import Callable

    from canonic.knowledge.models import KnowledgePage


def test_summary_match_outranks_body_match(
    make_search_page: Callable[..., KnowledgePage],
) -> None:
    """A term in the boosted ``summary`` field ranks above the same term in ``body``."""
    in_summary = make_search_page("in-summary", summary="alpha", body="filler text here")
    in_body = make_search_page("in-body", summary="something else", body="alpha appears here")
    index = KnowledgeIndex.build([in_body, in_summary])

    hits = index.search("alpha", limit=10)

    assert [h.id for h in hits] == ["in-summary", "in-body"]
    assert hits[0].rank == 0


def test_tag_match_is_searchable(make_search_page: Callable[..., KnowledgePage]) -> None:
    """A term present only in ``tags`` is found."""
    page = make_search_page("tagged", summary="", tags=["finance"], body="")
    index = KnowledgeIndex.build([page])

    hits = index.search("finance", limit=10)

    assert [h.id for h in hits] == ["tagged"]
    assert hits[0].scope is KnowledgeScope.GLOBAL
    assert hits[0].doc_key == "global:tagged"


def test_empty_query_returns_no_hits(make_search_page: Callable[..., KnowledgePage]) -> None:
    """A blank query degrades to an empty list rather than raising (§5.2)."""
    index = KnowledgeIndex.build([make_search_page("p", body="anything")])

    assert index.search("   ", limit=10) == []


def test_empty_corpus_is_safe() -> None:
    """Searching an empty index never fails."""
    index = KnowledgeIndex.build([])

    assert index.search("anything", limit=10) == []


def test_persisted_index_under_path(
    tmp_path,
    make_search_page: Callable[..., KnowledgePage],
) -> None:
    """A path-backed index builds the directory and searches like the RAM one."""
    index_dir = tmp_path / ".canonic" / "index" / "knowledge"
    index = KnowledgeIndex.build([make_search_page("p", summary="alpha")], path=index_dir)

    assert index_dir.exists()
    assert [h.id for h in index.search("alpha", limit=10)] == ["p"]
