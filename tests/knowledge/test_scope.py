"""Tests for path-based scope visibility and the strict-additive collision rule (GH-49)."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from canonic.exc import KnowledgePageError
from canonic.knowledge.loader import user_from_path
from canonic.knowledge.models import KnowledgeScope
from canonic.knowledge.scope import CollisionResult, ScopeResolver

if TYPE_CHECKING:
    from collections.abc import Callable

    from canonic.knowledge.models import KnowledgePage


# --- user_from_path -------------------------------------------------------------------------


def test_user_from_path_global_has_no_owner() -> None:
    assert user_from_path(Path("knowledge/global/customers-active.md")) is None


def test_user_from_path_returns_owner_id() -> None:
    assert user_from_path(Path("knowledge/user/alice/my-note.md")) == "alice"


def test_user_from_path_owner_with_subdirs() -> None:
    assert user_from_path(Path("knowledge/user/bob/sub/dir/note.md")) == "bob"


def test_user_from_path_missing_owner_segment_raises() -> None:
    with pytest.raises(KnowledgePageError, match="no '<id>' owner segment"):
        user_from_path(Path("knowledge/user/note.md"))


# --- visible_pages --------------------------------------------------------------------------


def test_visible_pages_global_only_returns_no_user_pages(
    make_page: Callable[..., KnowledgePage],
) -> None:
    corpus = [make_page("g1"), make_page("g2")]
    assert ScopeResolver().visible_pages("alice", corpus) == corpus


def test_visible_pages_user_sees_global_plus_own_not_others(
    make_page: Callable[..., KnowledgePage],
) -> None:
    g = make_page("shared")
    alice = make_page("alice-note", scope=KnowledgeScope.USER, user="alice")
    bob = make_page("bob-note", scope=KnowledgeScope.USER, user="bob")

    visible = ScopeResolver().visible_pages("alice", [g, alice, bob])

    assert g in visible
    assert alice in visible
    assert bob not in visible  # S4 AC2: never another user's page


def test_visible_pages_preserves_order(
    make_page: Callable[..., KnowledgePage],
) -> None:
    g1 = make_page("g1")
    alice = make_page("a", scope=KnowledgeScope.USER, user="alice")
    bob = make_page("b", scope=KnowledgeScope.USER, user="bob")
    g2 = make_page("g2")

    assert ScopeResolver().visible_pages("alice", [g1, alice, bob, g2]) == [g1, alice, g2]


# --- resolve_collision ----------------------------------------------------------------------


def test_resolve_collision_global_authoritative_user_annotated(
    make_page: Callable[..., KnowledgePage],
) -> None:
    g = make_page("customers-active", scope=KnowledgeScope.GLOBAL)
    alice = make_page("customers-active", scope=KnowledgeScope.USER, user="alice")

    result = ScopeResolver().resolve_collision(g, alice)

    assert isinstance(result, CollisionResult)
    assert result.authoritative is g  # S4 AC1: global is authoritative, never replaced
    assert result.annotation is alice


def test_resolve_collision_rejects_non_global_authoritative(
    make_page: Callable[..., KnowledgePage],
) -> None:
    alice = make_page("x", scope=KnowledgeScope.USER, user="alice")
    bob = make_page("x", scope=KnowledgeScope.USER, user="bob")
    with pytest.raises(KnowledgePageError, match="must be GLOBAL"):
        ScopeResolver().resolve_collision(alice, bob)


def test_resolve_collision_rejects_global_annotation(
    make_page: Callable[..., KnowledgePage],
) -> None:
    g1 = make_page("x", scope=KnowledgeScope.GLOBAL)
    g2 = make_page("x", scope=KnowledgeScope.GLOBAL)
    with pytest.raises(KnowledgePageError, match="must be USER"):
        ScopeResolver().resolve_collision(g1, g2)
