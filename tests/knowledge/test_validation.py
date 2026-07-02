"""Acceptance-criteria tests for write-time reference validation (GH-47, SPEC-E6 §3.1)."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from canonic.exc import ErrorCode, KnowledgeReferenceError
from canonic.knowledge.models import KnowledgeScope
from canonic.knowledge.validation import EntityIndex, PageIndex, ReferenceValidator

if TYPE_CHECKING:
    from collections.abc import Callable

    from canonic.knowledge.models import KnowledgePage


# --- EntityIndex / PageIndex builders -------------------------------------------------


def test_entity_index_enumerates_sources_columns_measures_dimensions(
    entity_index: EntityIndex,
) -> None:
    assert "warehouse_pg.customers" in entity_index  # source
    assert "warehouse_pg.orders" in entity_index  # source
    assert "warehouse_pg.orders.amount" in entity_index  # column
    assert "warehouse_pg.orders.total_revenue" in entity_index  # measure
    assert "warehouse_pg.orders.order_status" in entity_index  # dimension
    assert "warehouse_pg.orders.nonexistent" not in entity_index


def test_page_index_visibility_rule() -> None:
    index = PageIndex(
        slugs_by_scope={
            KnowledgeScope.GLOBAL: frozenset({"g"}),
            KnowledgeScope.USER: frozenset({"u"}),
        }
    )
    # GLOBAL pages see only GLOBAL.
    assert index.is_visible("g", KnowledgeScope.GLOBAL)
    assert not index.is_visible("u", KnowledgeScope.GLOBAL)
    # USER pages see GLOBAL and USER (strictly additive).
    assert index.is_visible("g", KnowledgeScope.USER)
    assert index.is_visible("u", KnowledgeScope.USER)
    assert not index.is_visible("missing", KnowledgeScope.USER)


# --- validate_page: the happy path (S1 AC1) -------------------------------------------


def test_valid_page_passes(
    entity_index: EntityIndex,
    page_index: PageIndex,
    make_page: Callable[..., KnowledgePage],
) -> None:
    page = make_page(
        sl_refs=["warehouse_pg.customers", "warehouse_pg.orders.total_revenue"],
        refs=["test-account-policy"],
        body="See [[test-account-policy]] and {{ sl:warehouse_pg.orders.total_revenue.expr }}.",
    )
    validator = ReferenceValidator(entity_index, page_index)
    assert validator.validate_page(page) is None


# --- broken sl_refs (S1 AC2) ----------------------------------------------------------


def test_broken_sl_ref_blocks_with_location(
    entity_index: EntityIndex,
    page_index: PageIndex,
    make_page: Callable[..., KnowledgePage],
) -> None:
    page = make_page(sl_refs=["warehouse_pg.orders.ghost_metric"])
    validator = ReferenceValidator(entity_index, page_index)

    with pytest.raises(KnowledgeReferenceError) as exc:
        validator.validate_page(page)

    err = exc.value
    assert err.kind == "sl_ref"
    assert err.ref == "warehouse_pg.orders.ghost_metric"
    assert err.path == ("sl_refs", 0)
    assert err.code is ErrorCode.VALIDATION_FAILED
    # Precise location: the page file and the offending ref text.
    assert str(page.path) in str(err)
    assert "warehouse_pg.orders.ghost_metric" in str(err)


# --- broken refs ----------------------------------------------------------------------


def test_broken_ref_blocks(
    entity_index: EntityIndex,
    page_index: PageIndex,
    make_page: Callable[..., KnowledgePage],
) -> None:
    page = make_page(refs=["no-such-page"])
    validator = ReferenceValidator(entity_index, page_index)

    with pytest.raises(KnowledgeReferenceError) as exc:
        validator.validate_page(page)
    assert exc.value.kind == "ref"
    assert exc.value.ref == "no-such-page"


def test_user_page_may_ref_global(
    entity_index: EntityIndex,
    page_index: PageIndex,
    make_page: Callable[..., KnowledgePage],
) -> None:
    page = make_page("my-private-note", scope=KnowledgeScope.USER, refs=["test-account-policy"])
    ReferenceValidator(entity_index, page_index).validate_page(page)  # no raise


def test_global_page_may_not_ref_user(
    entity_index: EntityIndex,
    page_index: PageIndex,
    make_page: Callable[..., KnowledgePage],
) -> None:
    page = make_page(scope=KnowledgeScope.GLOBAL, refs=["my-private-note"])
    with pytest.raises(KnowledgeReferenceError, match="visible from global scope"):
        ReferenceValidator(entity_index, page_index).validate_page(page)


# --- wikilinks in the body ------------------------------------------------------------


def test_broken_wikilink_blocks(
    entity_index: EntityIndex,
    page_index: PageIndex,
    make_page: Callable[..., KnowledgePage],
) -> None:
    page = make_page(body="Refers to [[ghost-page]] in prose.")
    validator = ReferenceValidator(entity_index, page_index)

    with pytest.raises(KnowledgeReferenceError) as exc:
        validator.validate_page(page)
    assert exc.value.kind == "wikilink"
    assert exc.value.ref == "ghost-page"


def test_valid_wikilink_with_alias_passes(
    entity_index: EntityIndex,
    page_index: PageIndex,
    make_page: Callable[..., KnowledgePage],
) -> None:
    page = make_page(body="See [[test-account-policy|the policy]] for details.")
    ReferenceValidator(entity_index, page_index).validate_page(page)  # no raise


def test_sl_template_tag_is_not_a_wikilink(
    entity_index: EntityIndex,
    page_index: PageIndex,
    make_page: Callable[..., KnowledgePage],
) -> None:
    # A {{ sl:… }} live-definition tag must not be parsed as a [[wikilink]].
    page = make_page(body="Revenue is {{ sl:warehouse_pg.orders.total_revenue.expr }}.")
    ReferenceValidator(entity_index, page_index).validate_page(page)  # no raise


# --- ordering -------------------------------------------------------------------------


def test_first_error_wins_sl_refs_before_refs(
    entity_index: EntityIndex,
    page_index: PageIndex,
    make_page: Callable[..., KnowledgePage],
) -> None:
    page = make_page(
        sl_refs=["warehouse_pg.orders.ghost_metric"],
        refs=["no-such-page"],
        body="[[also-missing]]",
    )
    with pytest.raises(KnowledgeReferenceError) as exc:
        ReferenceValidator(entity_index, page_index).validate_page(page)
    assert exc.value.kind == "sl_ref"  # sl_refs checked first
