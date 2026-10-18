"""Which knowledge pages a fired guardrail points at."""

from __future__ import annotations

from pathlib import Path

from canonic.compiler.result import FiredGuardrail
from canonic.knowledge.guardrail_refs import GuardrailKnowledgeLinker
from canonic.knowledge.models import KnowledgePage, KnowledgeScope

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
    linker = GuardrailKnowledgeLinker(
        [_page("refunds", STATUS), _page("revenue", REVENUE), _page("unrelated", "x.y.z")]
    )

    assert linker.refs_for(_guardrail(REVENUE, STATUS)) == ("refunds", "revenue")


def test_refs_are_sorted_and_deduplicated() -> None:
    linker = GuardrailKnowledgeLinker([_page("b", STATUS, REVENUE), _page("a", STATUS)])

    assert linker.refs_for(_guardrail(STATUS, REVENUE)) == ("a", "b")


def test_a_guardrail_without_overlap_has_no_refs() -> None:
    linker = GuardrailKnowledgeLinker([_page("refunds", STATUS)])

    assert linker.refs_for(_guardrail("warehouse_pg.orders.region")) == ()
    assert linker.refs_for(_guardrail()) == ()


def test_a_bare_source_in_sl_refs_does_not_match() -> None:
    linker = GuardrailKnowledgeLinker([_page("about-orders", "warehouse_pg.orders")])

    assert linker.refs_for(_guardrail(REVENUE, STATUS)) == ()


def test_declared_refs_replace_the_inferred_match() -> None:
    linker = GuardrailKnowledgeLinker([_page("refunds", STATUS), _page("policy")])

    assert linker.refs_for(_guardrail(STATUS, declared=("policy",))) == ("policy",)


def test_declared_refs_to_unknown_pages_are_dropped() -> None:
    linker = GuardrailKnowledgeLinker([_page("policy")])

    assert linker.refs_for(_guardrail(declared=("policy", "missing"))) == ("policy",)


def test_link_returns_the_guardrails_with_refs_filled_in() -> None:
    linker = GuardrailKnowledgeLinker([_page("refunds", STATUS)])
    guardrail = _guardrail(STATUS)

    (linked,) = linker.link([guardrail])

    assert linked.knowledge_refs == ("refunds",)
    assert guardrail.knowledge_refs == ()
