"""Which knowledge pages a fired guardrail or a metric points at."""

from __future__ import annotations

from pathlib import Path

import pytest

from canonic.compiler.result import FiredGuardrail
from canonic.knowledge.linker import KnowledgeLinker
from canonic.knowledge.models import KnowledgePage, KnowledgeScope, UsageMode

STATUS = "warehouse_pg.orders.status"
REVENUE = "warehouse_pg.orders.total_revenue"


def _page(page_id: str, *sl_refs: str) -> KnowledgePage:
    return KnowledgePage(
        id=page_id,
        path=Path(f"knowledge/global/{page_id}.md"),
        scope=KnowledgeScope.GLOBAL,
        sl_refs=list(sl_refs),
    )


def _guardrail(*entities: str, declared: tuple[str, ...] = ()) -> FiredGuardrail:
    return FiredGuardrail(
        id="g", kind="mandatory_filter", entities=entities, declared_knowledge_refs=declared
    )


def test_pages_sharing_an_entity_with_the_guardrail_match() -> None:
    linker = KnowledgeLinker(
        [_page("refunds", STATUS), _page("revenue", REVENUE), _page("unrelated", "x.y.z")]
    )

    assert linker.refs_for(_guardrail(REVENUE, STATUS)) == ("refunds", "revenue")


def test_refs_are_sorted_and_deduplicated() -> None:
    linker = KnowledgeLinker([_page("b", STATUS, REVENUE), _page("a", STATUS)])

    assert linker.refs_for(_guardrail(STATUS, REVENUE)) == ("a", "b")


def test_a_guardrail_without_overlap_has_no_refs() -> None:
    linker = KnowledgeLinker([_page("refunds", STATUS)])

    assert linker.refs_for(_guardrail("warehouse_pg.orders.region")) == ()
    assert linker.refs_for(_guardrail()) == ()


def test_a_bare_source_in_sl_refs_does_not_match() -> None:
    linker = KnowledgeLinker([_page("about-orders", "warehouse_pg.orders")])

    assert linker.refs_for(_guardrail(REVENUE, STATUS)) == ()


def test_declared_refs_replace_the_inferred_match() -> None:
    linker = KnowledgeLinker([_page("refunds", STATUS), _page("policy")])

    assert linker.refs_for(_guardrail(STATUS, declared=("policy",))) == ("policy",)


def test_declared_refs_to_unknown_pages_are_dropped() -> None:
    linker = KnowledgeLinker([_page("policy")])

    assert linker.refs_for(_guardrail(declared=("policy", "missing"))) == ("policy",)


def test_link_returns_the_guardrails_with_refs_filled_in() -> None:
    linker = KnowledgeLinker([_page("refunds", STATUS)])
    guardrail = _guardrail(STATUS)

    (linked,) = linker.link([guardrail])

    assert linked.knowledge_refs == ("refunds",)
    assert guardrail.knowledge_refs == ()


def _metric_page(
    page_id: str,
    *,
    metrics: tuple[str, ...] = (),
    sl_refs: tuple[str, ...] = (),
    usage_mode: UsageMode = UsageMode.DEFINITION,
) -> KnowledgePage:
    return KnowledgePage(
        id=page_id,
        path=Path(f"knowledge/global/{page_id}.md"),
        scope=KnowledgeScope.GLOBAL,
        metrics=list(metrics),
        sl_refs=list(sl_refs),
        usage_mode=usage_mode,
    )


def test_a_declared_page_decides_alone() -> None:
    linker = KnowledgeLinker(
        [
            _metric_page("declared"),
            _metric_page("named", metrics=("revenue",)),
            _metric_page("overlap", sl_refs=(STATUS,)),
        ]
    )

    assert linker.refs_for_metric("revenue", ["declared"], [STATUS]) == ("declared",)


def test_declared_ids_the_caller_cannot_see_are_dropped_without_falling_through() -> None:
    linker = KnowledgeLinker([_metric_page("named", metrics=("revenue",))])

    assert linker.refs_for_metric("revenue", ["hidden", "missing"], []) == ()


def test_pages_that_name_the_metric_come_before_entity_overlap() -> None:
    linker = KnowledgeLinker(
        [
            _metric_page("b", metrics=("revenue",)),
            _metric_page("a", metrics=("revenue", "orders")),
            _metric_page("other", metrics=("orders",)),
            _metric_page("overlap", sl_refs=(STATUS,)),
        ]
    )

    assert linker.refs_for_metric("revenue", [], [STATUS]) == ("a", "b")


def test_without_declared_or_named_pages_the_filter_entities_decide() -> None:
    linker = KnowledgeLinker([_metric_page("overlap", sl_refs=(STATUS,))])

    assert linker.refs_for_metric("revenue", [], [STATUS]) == ("overlap",)


@pytest.mark.parametrize("usage_mode", [UsageMode.DEFINITION, UsageMode.CAVEAT])
def test_definition_and_caveat_pages_overlap(usage_mode: UsageMode) -> None:
    linker = KnowledgeLinker([_metric_page("p", sl_refs=(STATUS,), usage_mode=usage_mode)])

    assert linker.refs_for_metric("revenue", [], [STATUS]) == ("p",)


@pytest.mark.parametrize("usage_mode", [UsageMode.REFERENCE, UsageMode.POLICY])
def test_reference_and_policy_pages_do_not_overlap(usage_mode: UsageMode) -> None:
    linker = KnowledgeLinker([_metric_page("p", sl_refs=(STATUS,), usage_mode=usage_mode)])

    assert linker.refs_for_metric("revenue", [], [STATUS]) == ()


def test_a_page_on_the_measure_alone_matches_nothing() -> None:
    linker = KnowledgeLinker([_metric_page("about-revenue", sl_refs=(REVENUE,))])

    assert linker.refs_for_metric("revenue", [], [STATUS]) == ()


def test_a_metric_with_no_filter_and_no_page_has_no_refs() -> None:
    assert (
        KnowledgeLinker([_metric_page("p", sl_refs=(STATUS,))]).refs_for_metric("revenue", [], [])
        == ()
    )
