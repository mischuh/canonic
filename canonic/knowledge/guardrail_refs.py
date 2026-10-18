"""Which knowledge pages explain a fired guardrail."""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable

    from canonic.compiler.result import FiredGuardrail
    from canonic.knowledge.models import KnowledgePage

__all__ = ["GuardrailKnowledgeLinker"]


class GuardrailKnowledgeLinker:
    """Resolves the knowledge pages for fired guardrails from a set of visible pages.

    A guardrail that names ``knowledge_refs`` in its contract gets exactly those pages, dropping
    any that are not among the visible pages, so an id never reaches a caller who could not read
    the page. Without that, the pages whose ``sl_refs`` overlap the entities the guardrail reads
    are returned. Either way the ids are sorted, so the same project gives the same answer.
    """

    def __init__(self, pages: Iterable[KnowledgePage]) -> None:
        self._ids = frozenset(page.id for page in pages)
        self._ids_by_entity: dict[str, set[str]] = {}
        for page in pages:
            for ref in page.sl_refs:
                self._ids_by_entity.setdefault(ref, set()).add(page.id)

    def refs_for(self, guardrail: FiredGuardrail) -> tuple[str, ...]:
        """The ids of the visible pages that explain ``guardrail``."""
        if guardrail.declared_knowledge_refs:
            return tuple(sorted(set(guardrail.declared_knowledge_refs) & self._ids))
        matched: set[str] = set()
        for entity in guardrail.entities:
            matched |= self._ids_by_entity.get(entity, set())
        return tuple(sorted(matched))

    def link(self, guardrails: Iterable[FiredGuardrail]) -> list[FiredGuardrail]:
        """``guardrails`` with ``knowledge_refs`` filled in."""
        return [replace(g, knowledge_refs=self.refs_for(g)) for g in guardrails]
