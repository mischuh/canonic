"""Unit tests for the hybrid retrieval engine (SPEC-E6 §5, §10)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from canon.knowledge.models import KnowledgeScope, UsageMode
from canon.knowledge.results import MatchedOn
from canon.knowledge.retrieval import KnowledgeSearch

if TYPE_CHECKING:
    from collections.abc import Callable

    from canon.knowledge.models import KnowledgePage
    from tests.knowledge.conftest import KeywordEmbedder


def test_lexical_only_returns_sensible_hits(
    make_search_page: Callable[..., KnowledgePage],
) -> None:
    """With no embedder, a query matching a page surfaces it with matched_on=[LEXICAL] (S2 AC2)."""
    sales = make_search_page("sales-definition", summary="sales report", body="quarterly sales")
    weather = make_search_page("weather", summary="weather forecast", body="rain and sun")
    engine = KnowledgeSearch([sales, weather])  # embedder omitted → lexical-only

    result = engine.search("sales", requesting_user="alice")

    assert [h.page for h in result.hits] == ["sales-definition"]
    assert result.hits[0].matched_on == [MatchedOn.LEXICAL]


def test_embeddings_off_never_fails(
    make_search_page: Callable[..., KnowledgePage],
) -> None:
    """A query with no matches returns an empty result, not an exception (S2 AC2)."""
    engine = KnowledgeSearch([make_search_page("p", summary="sales")])

    result = engine.search("nonexistent-term", requesting_user="alice")

    assert result.hits == []


def test_fused_results_set_matched_on(
    make_search_page: Callable[..., KnowledgePage],
    keyword_embedder: KeywordEmbedder,
) -> None:
    """Both-arm hits report [LEXICAL, VECTOR]; a vector-only hit reports [VECTOR] (S2 AC1)."""
    # "sales" appears literally → lexical + vector hit.
    both = make_search_page("sales-definition", summary="sales report", body="quarterly sales")
    # "revenue" is a vector synonym of "sales" but shares no literal token → vector only.
    vector_only = make_search_page("revenue-note", summary="revenue overview", body="earnings")
    # Unrelated → neither arm.
    weather = make_search_page("weather", summary="weather forecast", body="rain")
    engine = KnowledgeSearch([both, vector_only, weather], embedder=keyword_embedder)

    result = engine.search("sales", requesting_user="alice")

    by_page = {h.page: h for h in result.hits}
    assert set(by_page) == {"sales-definition", "revenue-note"}
    assert by_page["sales-definition"].matched_on == [MatchedOn.LEXICAL, MatchedOn.VECTOR]
    assert by_page["revenue-note"].matched_on == [MatchedOn.VECTOR]
    # The both-arms hit fuses higher than the single-arm one.
    assert result.hits[0].page == "sales-definition"


def test_tie_break_is_stable(
    make_search_page: Callable[..., KnowledgePage],
) -> None:
    """Equal-scoring pages order by stable page id, reproducibly across calls (§10)."""
    zzz = make_search_page("zzz", summary="sales", body="sales")
    aaa = make_search_page("aaa", summary="sales", body="sales")
    engine = KnowledgeSearch([zzz, aaa])

    first = engine.search("sales", requesting_user="alice")
    second = engine.search("sales", requesting_user="alice")

    assert [h.page for h in first.hits] == ["aaa", "zzz"]
    assert [h.page for h in first.hits] == [h.page for h in second.hits]


def test_tags_filter_applies(
    make_search_page: Callable[..., KnowledgePage],
) -> None:
    """Only pages sharing a requested tag are returned."""
    finance = make_search_page("finance-page", summary="sales", tags=["finance"])
    ops = make_search_page("ops-page", summary="sales", tags=["ops"])
    engine = KnowledgeSearch([finance, ops])

    result = engine.search("sales", requesting_user="alice", tags=["finance"])

    assert [h.page for h in result.hits] == ["finance-page"]


def test_usage_mode_filter_applies(
    make_search_page: Callable[..., KnowledgePage],
) -> None:
    """Only pages with the requested usage_mode are returned."""
    policy = make_search_page("policy-page", summary="sales", usage_mode=UsageMode.POLICY)
    reference = make_search_page("ref-page", summary="sales", usage_mode=UsageMode.REFERENCE)
    engine = KnowledgeSearch([policy, reference])

    result = engine.search("sales", requesting_user="alice", usage_mode=UsageMode.POLICY)

    assert [h.page for h in result.hits] == ["policy-page"]


def test_scope_filter_hides_other_users_pages(
    make_search_page: Callable[..., KnowledgePage],
) -> None:
    """A user sees their own user pages and globals, never another user's (S4 AC2)."""
    mine = make_search_page("mine", scope=KnowledgeScope.USER, user="alice", summary="sales")
    theirs = make_search_page("theirs", scope=KnowledgeScope.USER, user="bob", summary="sales")
    shared = make_search_page("shared", scope=KnowledgeScope.GLOBAL, summary="sales")
    engine = KnowledgeSearch([mine, theirs, shared])

    result = engine.search("sales", requesting_user="alice")

    assert {h.page for h in result.hits} == {"mine", "shared"}


def test_strict_additive_annotation(
    make_search_page: Callable[..., KnowledgePage],
) -> None:
    """A global + same-id user page yields one global hit carrying the user page (S4 AC1)."""
    global_page = make_search_page("metric-x", scope=KnowledgeScope.GLOBAL, summary="sales metric")
    user_page = make_search_page(
        "metric-x", scope=KnowledgeScope.USER, user="alice", summary="sales metric"
    )
    engine = KnowledgeSearch([global_page, user_page])

    result = engine.search("sales", requesting_user="alice")

    assert len(result.hits) == 1
    hit = result.hits[0]
    assert hit.page == "metric-x"
    assert hit.scope is KnowledgeScope.GLOBAL
    assert [(a.page, a.scope) for a in hit.annotations] == [("metric-x", "user:alice")]


def test_result_carries_empty_traversed(
    make_search_page: Callable[..., KnowledgePage],
) -> None:
    """The reserved graph-traversal field is present and empty until §6 lands."""
    engine = KnowledgeSearch([make_search_page("p", summary="sales")])

    result = engine.search("sales", requesting_user="alice")

    assert result.traversed == []
