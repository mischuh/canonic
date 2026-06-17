"""Path-based scope visibility and the strict-additive collision rule (SPEC-E6 §4).

Knowledge pages live in two path-defined scopes: ``knowledge/global/`` (shared, authoritative)
and ``knowledge/user/<id>/`` (personal). This module enforces §4's two guarantees at retrieval
time:

- **Visibility** — a user's search sees ``global`` pages + their *own* ``user/<id>`` pages,
  never another user's (S4 AC2). This is path-based visibility derived from the page path
  (:func:`~canon.knowledge.loader.user_from_path`), **not** row-level security; a real trust
  boundary and multi-user enforcement are E12 / Phase 2.
- **Strict-additive collision** — on a name/topic collision the global page is always
  authoritative; the colliding user page is surfaced as a *personal annotation attached to
  it*, never as a replacement (S4 AC1).

:class:`ScopeResolver` is a pure policy object: it filters and pairs frozen pages but never
mutates or writes them. The current-user identity is passed in by the caller (the retrieval
engine sources it from runtime config; SPEC-E6 §12).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict

from canon.exc import KnowledgePageError
from canon.knowledge.loader import user_from_path
from canon.knowledge.models import KnowledgePage, KnowledgeScope

if TYPE_CHECKING:
    from collections.abc import Iterable

__all__ = [
    "CollisionResult",
    "ScopeResolver",
]


class CollisionResult(BaseModel):
    """Outcome of a global/user name collision under the strict-additive rule (§4).

    The global page is always ``authoritative``; the colliding user page (if any) is carried
    as an ``annotation`` attached to it — never a replacement.
    """

    model_config = ConfigDict(frozen=True)

    authoritative: KnowledgePage
    annotation: KnowledgePage | None = None


class ScopeResolver:
    """Applies the SPEC-E6 §4 visibility and strict-additive collision rules."""

    def visible_pages(
        self, requesting_user: str, pages: Iterable[KnowledgePage]
    ) -> list[KnowledgePage]:
        """Pages ``requesting_user`` may see: all GLOBAL + only their own USER pages.

        Order-preserving. Another user's USER pages are dropped (S4 AC2). Operates over the
        page corpus rather than a :class:`~canon.knowledge.validation.PageIndex` because
        filtering requires each page's owner (parsed from its path via
        :func:`~canon.knowledge.loader.user_from_path`).
        """
        return [
            page
            for page in pages
            if page.scope is KnowledgeScope.GLOBAL or user_from_path(page.path) == requesting_user
        ]

    def resolve_collision(
        self, global_page: KnowledgePage, user_page: KnowledgePage
    ) -> CollisionResult:
        """Pair a colliding global/user page: global authoritative, user as annotation (S4 AC1).

        Raises KnowledgePageError if ``global_page`` is not GLOBAL or ``user_page`` is not
        USER — a user page can never be authoritative over a global one.
        """
        if global_page.scope is not KnowledgeScope.GLOBAL:
            raise KnowledgePageError(
                f"{global_page.path}: authoritative page must be GLOBAL, "
                f"got {global_page.scope.value}"
            )
        if user_page.scope is not KnowledgeScope.USER:
            raise KnowledgePageError(
                f"{user_page.path}: annotation page must be USER, got {user_page.scope.value}"
            )
        return CollisionResult(authoritative=global_page, annotation=user_page)
