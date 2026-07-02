"""Tests for graph traversal over the page reference graph (SPEC-E6 §6, S3 AC1)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from canonic.knowledge.models import KnowledgePage, KnowledgeScope
from canonic.knowledge.results import Hit
from canonic.knowledge.traversal import GraphTraversal

if TYPE_CHECKING:
    from collections.abc import Callable

    from canonic.knowledge.validation import EntityIndex


def _seed(page: KnowledgePage) -> Hit:
    """A minimal search Hit pointing at ``page`` — what ``SearchResult.hits`` would carry."""
    return Hit(
        page=page.id,
        scope=page.scope,
        score=1.0,
        summary=page.summary,
        matched_on=[],
        sl_refs=page.sl_refs,
    )


def _ids(pages: list[KnowledgePage]) -> list[str]:
    return [p.id for p in pages]


def test_seed_expands_to_bounded_depth_as_one_deduped_subgraph(
    make_page: Callable[..., KnowledgePage],
) -> None:
    """A seed with refs expands BFS to bounded depth as one deduped subgraph (S3 AC1)."""
    seed = make_page("active-customer", refs=["caveat", "policy"])
    caveat = make_page("caveat", refs=["policy"])  # also reachable from the seed
    policy = make_page("policy")
    traversal = GraphTraversal([seed, caveat, policy])

    result = traversal.expand([_seed(seed)], max_depth=2, max_nodes=10)

    # Seed + both neighbors, each exactly once despite the diamond (seed→caveat→policy
    # and seed→policy).
    assert _ids(result.pages) == ["active-customer", "caveat", "policy"]


def test_depth_bound_stops_the_walk(make_page: Callable[..., KnowledgePage]) -> None:
    """Pages beyond ``max_depth`` edges from a seed are not pulled in."""
    a = make_page("a", refs=["b"])
    b = make_page("b", refs=["c"])
    c = make_page("c", refs=["d"])
    d = make_page("d")
    traversal = GraphTraversal([a, b, c, d])

    result = traversal.expand([_seed(a)], max_depth=1, max_nodes=10)

    assert _ids(result.pages) == ["a", "b"]  # c is at depth 2, d at depth 3


def test_node_cap_respected(make_page: Callable[..., KnowledgePage]) -> None:
    """A wide fan-out is truncated to ``max_nodes`` (S3: node cap prevents whole-graph pull)."""
    seed = make_page("hub", refs=[f"n{i}" for i in range(10)])
    leaves = [make_page(f"n{i}") for i in range(10)]
    traversal = GraphTraversal([seed, *leaves])

    result = traversal.expand([_seed(seed)], max_depth=3, max_nodes=4)

    assert len(result.pages) == 4
    assert result.pages[0].id == "hub"  # seed admitted first


def test_cycle_does_not_loop_forever(make_page: Callable[..., KnowledgePage]) -> None:
    """A reference cycle terminates and yields each node once (S3: cycle safety)."""
    a = make_page("a", refs=["b"])
    b = make_page("b", refs=["a"])
    traversal = GraphTraversal([a, b])

    result = traversal.expand([_seed(a)], max_depth=5, max_nodes=10)

    assert _ids(result.pages) == ["a", "b"]


def test_wikilinks_are_followed_and_unioned_with_refs(
    make_page: Callable[..., KnowledgePage],
) -> None:
    """Body ``[[links]]`` are traversed alongside ``refs`` as one neighbor set (S3 AC1)."""
    seed = make_page("note", refs=["policy"], body="see [[caveat]] and [[policy|the policy]]")
    caveat = make_page("caveat")
    policy = make_page("policy")
    traversal = GraphTraversal([seed, caveat, policy])

    result = traversal.expand([_seed(seed)], max_depth=1, max_nodes=10)

    # [[policy]] and refs=["policy"] dedupe to one; [[caveat]] is followed too.
    assert _ids(result.pages) == ["note", "caveat", "policy"]


def test_traversal_runs_without_search_or_embedder(
    make_page: Callable[..., KnowledgePage],
) -> None:
    """Traversal needs only the loaded pages — no embedder, no index (S3: no re-search)."""
    seed = make_page("a", refs=["b"])
    b = make_page("b")

    result = GraphTraversal([seed, b]).expand([_seed(seed)], max_depth=1, max_nodes=10)

    assert _ids(result.pages) == ["a", "b"]


def test_entities_collected_and_filtered_to_live(
    make_page: Callable[..., KnowledgePage],
    entity_index: EntityIndex,
) -> None:
    """Reached ``sl_refs`` surface as entities; an ``entity_index`` drops stale targets."""
    seed = make_page(
        "active-customer",
        refs=["policy"],
        sl_refs=["warehouse_pg.customers", "warehouse_pg.gone"],  # second is stale
    )
    policy = make_page("policy", sl_refs=["warehouse_pg.orders.total_revenue"])
    traversal = GraphTraversal([seed, policy], entity_index=entity_index)

    result = traversal.expand([_seed(seed)], max_depth=1, max_nodes=10)

    assert result.entities == [
        "warehouse_pg.customers",
        "warehouse_pg.orders.total_revenue",
    ]


def test_entities_unfiltered_without_index(
    make_page: Callable[..., KnowledgePage],
) -> None:
    """Without an entity index, every reached ``sl_ref`` is reported (sorted, deduped)."""
    seed = make_page("a", sl_refs=["z.entity", "a.entity", "z.entity"])
    traversal = GraphTraversal([seed])

    result = traversal.expand([_seed(seed)], max_depth=0, max_nodes=10)

    assert result.entities == ["a.entity", "z.entity"]


def test_slug_collision_resolves_global_authoritative(
    make_page: Callable[..., KnowledgePage],
) -> None:
    """A ref to a colliding slug resolves to the GLOBAL page, never the user one (§4)."""
    seed = make_page("seed", scope=KnowledgeScope.USER, refs=["shared"])
    global_shared = make_page("shared", scope=KnowledgeScope.GLOBAL)
    user_shared = make_page("shared", scope=KnowledgeScope.USER)
    traversal = GraphTraversal([seed, user_shared, global_shared])

    result = traversal.expand([_seed(seed)], max_depth=1, max_nodes=10)

    reached = next(p for p in result.pages if p.id == "shared")
    assert reached.scope is KnowledgeScope.GLOBAL


def test_ordering_is_deterministic(make_page: Callable[..., KnowledgePage]) -> None:
    """Identical inputs yield identical page and entity ordering across runs (§10)."""
    seed = make_page("seed", refs=["m", "a", "z"], sl_refs=["z.e", "a.e"])
    pages = [seed, make_page("z"), make_page("a"), make_page("m")]
    traversal = GraphTraversal(pages)

    first = traversal.expand([_seed(seed)], max_depth=1, max_nodes=10)
    second = traversal.expand([_seed(seed)], max_depth=1, max_nodes=10)

    assert _ids(first.pages) == _ids(second.pages) == ["seed", "a", "m", "z"]
    assert first.entities == second.entities == ["a.e", "z.e"]


def test_seed_also_a_neighbor_appears_once(
    make_page: Callable[..., KnowledgePage],
) -> None:
    """A seed that is also another seed's neighbor is admitted a single time."""
    a = make_page("a", refs=["b"])
    b = make_page("b")
    traversal = GraphTraversal([a, b])

    result = traversal.expand([_seed(a), _seed(b)], max_depth=1, max_nodes=10)

    assert _ids(result.pages) == ["a", "b"]
