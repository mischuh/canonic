"""Knowledge-page reference upkeep during ingest (SPEC-E6 §3.2, §8).

After reconciliation has decided the semantic state of a run, every knowledge page on disk is
checked against the state that run leaves behind:

- A page whose ``sl_refs`` or ``refs`` no longer resolve gets a propose-only ``EDIT`` entry
  that removes them and downgrades its freshness (:class:`PruneAdvisor` computes the removal).
  The file is never edited in place and a frozen page is left alone.
- A page whose references all resolve is *validated*: :meth:`PageReferenceCheck.stamp` refreshes
  ``meta.last_validated_at``, the stamp ``read_knowledge_page`` turns into a staleness signal.

Pages the run already decided (an ingest-drafted page that was re-rendered or pruned) are
skipped, reconciliation owns them.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import ValidationError

from canonic.exc import KnowledgePageError
from canonic.ingestion.candidates import final_sources
from canonic.ingestion.models import (
    ProposalOp,
    ReconciliationDecision,
    ReconciliationEntry,
    ReconciliationReport,
)
from canonic.knowledge.loader import (
    dump_knowledge_page,
    load_knowledge_page,
    scope_from_path,
    slug_from_path,
)
from canonic.knowledge.pruning import PruneAdvisor
from canonic.knowledge.validation import EntityIndex, PageIndex
from canonic.semantic.models import SemanticSource

if TYPE_CHECKING:
    from canonic.ingestion.reconciliation import AcceptedStore
    from canonic.knowledge.models import KnowledgePage

logger = logging.getLogger(__name__)

__all__ = ["PageReferenceCheck", "PageReferenceChecker"]

#: A validated page is re-stamped at most this often, so an ingest loop does not rewrite
#: every page (and dirty git) on each run.
_RESTAMP_AFTER = timedelta(days=1)


class PageReferenceCheck:
    """The outcome of checking every knowledge page against a run's resulting state.

    ``entries`` are the propose-only prune edits, ``validated`` the pages whose references all
    resolve and whose stamp is missing or older than a day.
    """

    def __init__(self, entries: list[ReconciliationEntry], validated: list[KnowledgePage]) -> None:
        self.entries = entries
        self.validated = validated

    def stamp(self, now: datetime | None = None) -> None:
        """Write ``meta.last_validated_at`` onto each validated page (in-place freshness stamp).

        The same carve-out as the semantic-source no-op refresh (SPEC-E4 §5.2 / §7): it touches
        no proposal and no decision.
        """
        stamped_at = now or datetime.now(UTC)
        for page in self.validated:
            meta = page.meta.model_copy(update={"last_validated_at": stamped_at})
            page.path.write_text(dump_knowledge_page(page.model_copy(update={"meta": meta})))


class PageReferenceChecker:
    """Checks the knowledge pages under ``project_root`` against a reconciliation report."""

    def __init__(self, project_root: Path, advisor: PruneAdvisor | None = None) -> None:
        self._root = project_root
        self._advisor = advisor or PruneAdvisor()

    def check(self, report: ReconciliationReport, accepted: AcceptedStore) -> PageReferenceCheck:
        """Compare every page's references with the state ``report`` leaves behind."""
        knowledge_root = self._root / "knowledge"
        if not knowledge_root.is_dir():
            return PageReferenceCheck([], [])

        pages = self._load_pages(knowledge_root)
        decided = {entry.target for entry in report.entries}
        entity_index = self._entity_index(report, accepted)
        page_index = self._page_index(pages, report)

        entries: list[ReconciliationEntry] = []
        validated: list[KnowledgePage] = []
        now = datetime.now(UTC)
        for page in pages:
            target = page.path.relative_to(self._root).as_posix()
            if target in decided or page.meta.frozen:
                continue
            stale_sl = self._advisor.stale_sl_refs(page, entity_index)
            stale_refs = self._advisor.stale_refs(page, page_index)
            if stale_sl or stale_refs:
                entries.append(self._prune_entry(page, target, stale_sl, stale_refs))
            elif (
                page.meta.last_validated_at is None
                or now - page.meta.last_validated_at > _RESTAMP_AFTER
            ):
                validated.append(page)
        return PageReferenceCheck(entries, validated)

    @staticmethod
    def _load_pages(knowledge_root: Path) -> list[KnowledgePage]:
        """Every loadable page, a page that fails to load is left to ``canonic validate``."""
        pages: list[KnowledgePage] = []
        for path in sorted(knowledge_root.rglob("*.md")):
            try:
                pages.append(load_knowledge_page(path))
            except KnowledgePageError:
                logger.debug("skipping unloadable knowledge page %s", path)
        return pages

    @staticmethod
    def _entity_index(report: ReconciliationReport, accepted: AcceptedStore) -> EntityIndex:
        """Entities of the post-ingest semantic state: accepted sources, plus the run's changes."""
        contents = final_sources(accepted, report.entries)
        for entry in report.entries:
            if entry.decision is ReconciliationDecision.PRUNE and entry.target.startswith(
                "semantics/"
            ):
                contents.pop(str((entry.existing or {}).get("name")), None)
        sources: list[SemanticSource] = []
        for content in contents.values():
            try:
                sources.append(SemanticSource.model_validate(content))
            except ValidationError:
                logger.debug("skipping semantic source that does not validate: %r", content)
        return EntityIndex.from_sources(sources)

    def _page_index(self, pages: list[KnowledgePage], report: ReconciliationReport) -> PageIndex:
        """Pages of the post-ingest state: those on disk, minus pruned, plus newly drafted."""
        pruned = {e.target for e in report.entries if e.decision is ReconciliationDecision.PRUNE}
        kept = [p for p in pages if p.path.relative_to(self._root).as_posix() not in pruned]
        by_scope = {
            scope: set(slugs) for scope, slugs in PageIndex.from_pages(kept).slugs_by_scope.items()
        }
        for entry in report.entries:
            if entry.decision is ReconciliationDecision.ADD and entry.target.startswith(
                "knowledge/"
            ):
                path = Path(entry.target)
                by_scope.setdefault(scope_from_path(path), set()).add(slug_from_path(path))
        return PageIndex(slugs_by_scope={s: frozenset(v) for s, v in by_scope.items()})

    def _prune_entry(
        self, page: KnowledgePage, target: str, stale_sl: list[str], stale_refs: list[str]
    ) -> ReconciliationEntry:
        """A propose-only ``EDIT`` removing the dangling references from ``page``."""
        proposal = self._advisor.propose_prune(page, stale_sl, stale_refs)
        assert proposal is not None  # at least one stale reference, checked by the caller
        pruned = self._advisor.pruned_page(page, stale_sl, stale_refs)
        edit = proposal.model_copy(
            update={
                "op": ProposalOp.EDIT,
                "content": {"body": dump_knowledge_page(pruned)},
            }
        )
        removed = ", ".join([*stale_sl, *stale_refs])
        return ReconciliationEntry(
            decision=ReconciliationDecision.EDIT,
            target=target,
            proposal=edit,
            existing={"body": page.path.read_text()},
            existing_provenance=page.meta.provenance,
            recommended_action=f"referenced entity disappeared at ingest, remove {removed}",
        )
