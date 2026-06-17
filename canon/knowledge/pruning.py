"""Ingest-time reference pruning for knowledge pages (SPEC-E6 §3.2).

The write-time counterpart in :mod:`canon.knowledge.validation` blocks a page that points
at nothing. This module handles the opposite direction: a page that *was* valid but whose
target disappeared at ingest. When E4 detects that a referenced semantic entity (or page)
is gone, every page bound to it holds a dangling reference. SPEC-E6 §3.2 / SPEC-E4 §5.2
("target evidence disappeared") require the response to be a **propose-only diff** — the
stale ref is proposed for removal and the page's freshness is downgraded; the file is never
silently edited and a dangling ref is never left behind (S5 AC1).

:class:`PruneAdvisor` is a pure advisor: it computes the stale subset of a page's references
against the live indexes and emits an E4 :class:`~canon.ingestion.models.Proposal`. It writes
no file and makes no policy decision — ``frozen``/higher-tier handling belongs to the
reconciliation engine (SPEC-E4 §5.3), so the advisor always proposes and lets the engine
decide whether to flag instead.

Integration seam (not wired here): E4, per affected page during ingest, calls
:meth:`PruneAdvisor.stale_sl_refs` / :meth:`PruneAdvisor.stale_refs` against the post-ingest
indexes, then :meth:`PruneAdvisor.propose_prune`, feeding any non-``None`` proposal into the
standard reconciliation flow.

Body ``[[wikilinks]]`` are intentionally out of scope: they live in prose, not frontmatter,
and §3.2 scopes pruning to ``sl_refs`` and page ``refs``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from canon.ingestion.models import DraftedBy, Proposal, ProposalOp
from canon.semantic.models import Provenance

if TYPE_CHECKING:
    from canon.knowledge.models import KnowledgePage
    from canon.knowledge.validation import EntityIndex, PageIndex

__all__ = [
    "PruneAdvisor",
]

# Deterministic prune carries full certainty in the removal (SPEC-E4 §4); mirrors the
# semantic-source prune in canon.ingestion.reconciliation.
_PRUNE_CONFIDENCE = 1.0


class PruneAdvisor:
    """Computes propose-only prune diffs for pages with disappeared refs (SPEC-E6 §3.2)."""

    @staticmethod
    def stale_sl_refs(page: KnowledgePage, entity_index: EntityIndex) -> list[str]:
        """Return the ``sl_refs`` that no longer resolve to a live semantic entity.

        Order-preserving subset of ``page.sl_refs``; reuses
        :meth:`~canon.knowledge.validation.EntityIndex.__contains__`.
        """
        return [ref for ref in page.sl_refs if ref not in entity_index]

    @staticmethod
    def stale_refs(page: KnowledgePage, page_index: PageIndex) -> list[str]:
        """Return the page ``refs`` that no longer point at a visible page.

        Order-preserving subset of ``page.refs``; uses the same visibility rule as
        :class:`~canon.knowledge.validation.ReferenceValidator` so prune and validate agree.
        """
        return [ref for ref in page.refs if not page_index.is_visible(ref, page.scope)]

    def propose_prune(
        self, page: KnowledgePage, stale_sl: list[str], stale_refs: list[str]
    ) -> Proposal | None:
        """Emit a single propose-only prune diff for ``page``, or ``None`` if it is clean.

        The proposal removes ``stale_sl`` from ``sl_refs`` and ``stale_refs`` from ``refs``,
        downgrades ``meta.last_validated_at`` to ``None``, and anchors to the disappeared
        entities' fingerprints (those recorded in ``page.meta.bound_fingerprints``). It writes
        no file; reconciliation decides whether to apply or flag it (SPEC-E4 §5.2/§5.3).
        """
        if not stale_sl and not stale_refs:
            return None

        return Proposal(
            target=str(page.path),
            op=ProposalOp.PRUNE,
            content=self._proposed_frontmatter(page, stale_sl, stale_refs),
            provenance=Provenance.INFERRED,
            confidence=_PRUNE_CONFIDENCE,
            anchored_to=self._anchored_fingerprints(page, stale_sl),
            drafted_by=DraftedBy.DETERMINISTIC,
        )

    @staticmethod
    def _proposed_frontmatter(
        page: KnowledgePage, stale_sl: list[str], stale_refs: list[str]
    ) -> dict[str, Any]:
        """Build the page's post-prune frontmatter: stale refs removed, freshness downgraded.

        Mirrors the writable frontmatter only — the loader-derived ``id``/``path``/``scope``
        and the ``body`` are excluded (they are not frontmatter; SPEC-E6 §2).
        """
        stale_sl_set = set(stale_sl)
        stale_refs_set = set(stale_refs)
        meta = page.meta.model_dump(mode="json")
        meta["last_validated_at"] = None  # freshness downgrade (SPEC-E6 §7)
        return {
            "summary": page.summary,
            "tags": list(page.tags),
            "sl_refs": [ref for ref in page.sl_refs if ref not in stale_sl_set],
            "refs": [ref for ref in page.refs if ref not in stale_refs_set],
            "usage_mode": page.usage_mode.value,
            "meta": meta,
        }

    @staticmethod
    def _anchored_fingerprints(page: KnowledgePage, stale_sl: list[str]) -> list[str]:
        """Fingerprints of the disappeared entities, deduped and order-preserving.

        Stale page ``refs`` contribute nothing — pages are not fingerprinted.
        """
        fingerprints: list[str] = []
        for ref in stale_sl:
            fp = page.meta.bound_fingerprints.get(ref)
            if fp is not None and fp not in fingerprints:
                fingerprints.append(fp)
        return fingerprints
