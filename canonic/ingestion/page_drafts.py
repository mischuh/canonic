"""Knowledge pages drafted from doc evidence (Notion, URL, Ossie ``ai_context``).

The builder wraps every :class:`~canonic.connectors.base.DocEvidence` into a page draft
for ``knowledge/global/<slug>.md``, marked with :data:`PAGE_DRAFT_SENTINEL`. Its
``topic_refs`` are only candidates, and which semantic entities exist depends on the rest
of the run. So reconciliation renders the page after every semantic decision: candidates
that resolve against the resulting state become ``sl_refs``, the rest stay unresolved.

A drafted page records ``meta.source_fingerprint`` over the evidence and its resolved
references. That is what lets the next run recognise an unchanged page as a no-op, and
what marks the page as ingest's to reconcile at all.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import ValidationError

from canonic.connectors.base import DocEvidence
from canonic.ingestion.candidates import final_sources
from canonic.ingestion.models import DraftedBy, EvidenceItem, Proposal, ProposalOp
from canonic.knowledge.loader import dump_knowledge_page
from canonic.knowledge.models import (
    KnowledgePage,
    KnowledgePageMeta,
    KnowledgeScope,
    UsageMode,
)
from canonic.knowledge.resolve import resolve_topic_refs
from canonic.knowledge.validation import EntityIndex
from canonic.semantic.models import Provenance, SemanticSource

if TYPE_CHECKING:
    from canonic.ingestion.models import ReconciliationEntry
    from canonic.ingestion.reconciliation import AcceptedStore

__all__ = ["PAGE_DRAFT_SENTINEL", "draft_page", "is_page_draft", "render_page_drafts"]

#: Marks a proposal as an unrendered page draft carrying its doc evidence.
PAGE_DRAFT_SENTINEL = "_page_draft"

_SUMMARY_LIMIT = 200


def _slug(title: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    return slug or f"doc-{hashlib.sha256(title.encode()).hexdigest()[:8]}"


def _summary(body: str) -> str:
    flat = " ".join(body.split())
    if len(flat) <= _SUMMARY_LIMIT:
        return flat
    return f"{flat[:_SUMMARY_LIMIT].rsplit(' ', 1)[0]}…"


def is_page_draft(proposal: Proposal) -> bool:
    return PAGE_DRAFT_SENTINEL in proposal.content


def draft_page(item: EvidenceItem, *, confidence: float) -> Proposal | str:
    """Wrap one doc-evidence item into a page draft, or return why it cannot be drafted."""
    try:
        doc = DocEvidence.model_validate(item.payload)
    except ValidationError as exc:
        return f"invalid doc evidence payload: {exc}"
    return Proposal(
        target=f"knowledge/global/{_slug(doc.title)}.md",
        op=ProposalOp.ADD,
        content={PAGE_DRAFT_SENTINEL: doc.model_dump(mode="json")},
        provenance=Provenance.INFERRED,
        confidence=confidence,
        anchored_to=[doc.source_fingerprint] if doc.source_fingerprint else [],
        drafted_by=DraftedBy.DETERMINISTIC,
        acquisition_tier=item.acquisition_tier,
    )


def _semantic_sources(contents: dict[str, dict[str, Any]]) -> list[SemanticSource]:
    sources: list[SemanticSource] = []
    for name in sorted(contents):
        try:
            sources.append(SemanticSource.model_validate(contents[name]))
        except ValidationError:
            continue  # an invalid draft is the validation gate's to report
    return sources


def render_page_drafts(
    drafts: list[Proposal], accepted: AcceptedStore, entries: list[ReconciliationEntry]
) -> list[Proposal]:
    """Render each draft into its final page against the semantic state the run leaves.

    The returned proposals are ordinary ``knowledge/*.md`` proposals: ``content["body"]`` is
    the full file, ``content["meta"]["source_fingerprint"]`` drives no-op detection.
    """
    sources = _semantic_sources(final_sources(accepted, entries))
    entities = EntityIndex.from_sources(sources)
    return [_render(draft, sources, entities) for draft in drafts]


def _render(draft: Proposal, sources: list[SemanticSource], entities: EntityIndex) -> Proposal:
    doc = DocEvidence.model_validate(draft.content[PAGE_DRAFT_SENTINEL])
    resolved, unresolved = resolve_topic_refs(doc.topic_refs, sources)
    sl_refs = list(dict.fromkeys(resolved))
    bound = {ref: fp for ref in sl_refs if (fp := entities.current_fingerprint(ref)) is not None}
    fingerprint = (
        "sha256:"
        + hashlib.sha256(
            json.dumps(
                {"doc": doc.source_fingerprint, "sl_refs": sl_refs, "bound": bound}, sort_keys=True
            ).encode()
        ).hexdigest()
    )
    body = doc.body if doc.body.endswith("\n") else f"{doc.body}\n"
    page = KnowledgePage(
        id=Path(draft.target).stem,
        path=Path(draft.target),
        scope=KnowledgeScope.GLOBAL,
        summary=_summary(doc.body),
        sl_refs=sl_refs,
        usage_mode=UsageMode(doc.usage_hint.value),
        meta=KnowledgePageMeta(
            provenance=Provenance.INFERRED,
            bound_fingerprints=bound,
            source_fingerprint=fingerprint,
        ),
        body=body,
    )
    meta: dict[str, Any] = {"source_fingerprint": fingerprint}
    if unresolved:
        meta["unresolved_topic_refs"] = unresolved
    return draft.model_copy(update={"content": {"body": dump_knowledge_page(page), "meta": meta}})
