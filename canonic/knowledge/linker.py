"""Which knowledge pages explain a fired guardrail or define a metric."""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING

from canonic.knowledge.models import UsageMode

if TYPE_CHECKING:
    from collections.abc import Iterable

    from canonic.compiler.result import FiredGuardrail
    from canonic.knowledge.models import KnowledgePage

__all__ = ["KnowledgeLinker"]

#: The usage modes that say what a metric is, or what to watch for in it. A reference or policy
#: page that merely touches the same column does not help to choose between metrics.
_METRIC_USAGE_MODES = frozenset({UsageMode.DEFINITION, UsageMode.CAVEAT})


class KnowledgeLinker:
    """Resolves the knowledge pages for what is being explained, from a set of visible pages.

    Declared ids always win. A guardrail or a metric that names ``knowledge_refs`` in its contract
    gets exactly those pages, dropping any that are not among the visible pages, so an id never
    reaches a caller who could not read the page. Without declared ids, the pages are found from
    the page side: for a guardrail the pages whose ``sl_refs`` overlap the entities it reads, for
    a metric the pages that name it, and failing those the definition and caveat pages whose
    ``sl_refs`` overlap the entities of its filter. Either way the ids are sorted, so the same
    project gives the same answer.
    """

    def __init__(self, pages: Iterable[KnowledgePage]) -> None:
        pages = list(pages)
        self._ids = frozenset(page.id for page in pages)
        self._ids_by_entity: dict[str, set[str]] = {}
        self._definitions_by_entity: dict[str, set[str]] = {}
        self._ids_by_metric: dict[str, set[str]] = {}
        for page in pages:
            for ref in page.sl_refs:
                self._ids_by_entity.setdefault(ref, set()).add(page.id)
                if page.usage_mode in _METRIC_USAGE_MODES:
                    self._definitions_by_entity.setdefault(ref, set()).add(page.id)
            for metric in page.metrics:
                self._ids_by_metric.setdefault(metric, set()).add(page.id)

    def refs_for(self, guardrail: FiredGuardrail) -> tuple[str, ...]:
        """The ids of the visible pages that explain ``guardrail``."""
        if guardrail.declared_knowledge_refs:
            return tuple(sorted(set(guardrail.declared_knowledge_refs) & self._ids))
        return self._overlapping(guardrail.entities, self._ids_by_entity)

    def link(self, guardrails: Iterable[FiredGuardrail]) -> list[FiredGuardrail]:
        """``guardrails`` with ``knowledge_refs`` filled in."""
        return [replace(g, knowledge_refs=self.refs_for(g)) for g in guardrails]

    def refs_for_metric(
        self, metric: str, declared: Iterable[str], filter_entities: Iterable[str]
    ) -> tuple[str, ...]:
        """The ids of the visible pages that define or qualify ``metric``.

        In order: the ``declared`` ids, then the pages that name ``metric`` in their front
        matter, then the definition and caveat pages overlapping ``filter_entities``, the
        entities of the metric's own filter. The first step that applies decides, so a metric
        with a declared page is not widened by pages found some other way. The entities of the
        measure are deliberately not used: many metrics share one measure, and every page that
        touches it would match them all.
        """
        declared_ids = set(declared)
        if declared_ids:
            return tuple(sorted(declared_ids & self._ids))
        named = self._ids_by_metric.get(metric)
        if named:
            return tuple(sorted(named))
        return self._overlapping(filter_entities, self._definitions_by_entity)

    @staticmethod
    def _overlapping(
        entities: Iterable[str], ids_by_entity: dict[str, set[str]]
    ) -> tuple[str, ...]:
        matched: set[str] = set()
        for entity in entities:
            matched |= ids_by_entity.get(entity, set())
        return tuple(sorted(matched))
