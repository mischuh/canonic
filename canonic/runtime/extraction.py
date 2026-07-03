"""LLM-backed extraction skill for the generic evidence connector (SPEC-E10, E3 §5 amendment).

Bridges the fetch/extract split's :class:`~canonic.connectors.evidence.ExtractionSkill` seam
to a real :class:`~canonic.runtime.generation.GenerationRuntime`, the same way
:class:`~canonic.runtime.drafter.RuntimeLLMDrafter` bridges the E4 builder's ``LLMDrafter``
seam. Meant for prose sources with no structured classification fields of their own
(Confluence, Google Docs, ...) — Notion keeps its deterministic, property-based
``NotionExtractionSkill`` since it already carries an explicit classification (SPEC-E3 §10).

The connector factory only threads a bare ``Connection`` into connector builders (SPEC-E2
§2.2a), so it has no access to ``LLMConfig``. :func:`make_extraction_skill` is built
separately from ``config.llm``/``config.runtime`` (mirroring ``make_drafter``) and wired into
an already-constructed :class:`~canonic.connectors.evidence.GenericEvidenceConnector` via
``set_extraction_skill`` — the same "collaborator wired in downstream, not at factory-build
time" pattern this codebase already uses for ``LLMDrafter``/``ContextBuilder``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

from pydantic import BaseModel

from canonic.connectors.base import DocEvidence
from canonic.connectors.evidence import (
    ExtractionSkill,
    NullExtractionSkill,
    RawDoc,
    compute_doc_fingerprint,
    parse_usage_hint,
)
from canonic.runtime.resolver import Task

if TYPE_CHECKING:
    from canonic.airgap import EgressPolicy
    from canonic.config import LLMConfig, RuntimeConfig
    from canonic.runtime.generation import GenerationRuntime

__all__ = ["RuntimeExtractionSkill", "make_extraction_skill"]

_EXTRACTION_SYSTEM = (
    "You are classifying one internal document for a data team's knowledge base. Read the "
    "title and body and decide its usage_hint: 'policy' (a rule or standard that must be "
    "followed, e.g. how a metric should be computed or which rows to exclude), 'caveat' (a "
    "warning about a known data-quality issue, edge case, or limitation), 'definition' "
    "(defines what a business term or metric means), or 'reference' (general background "
    "information that fits none of the above — the default when unsure). Also list "
    "topic_refs: short candidate terms or entity names the document is about (e.g. table or "
    "metric names it discusses) — leave the list empty rather than guess at one. "
    "Respond only with the requested JSON object — no prose outside it."
)


class _ExtractionResponse(BaseModel):
    """Schema the model must satisfy when classifying a RawDoc."""

    usage_hint: str = "reference"
    topic_refs: list[str] = []
    reasoning: str = ""


def _extraction_prompt(doc: RawDoc) -> str:
    """Render a RawDoc's title/body into a classification prompt."""
    lines = [
        f"Title: {doc.title!r}",
        "",
        "Body:",
        doc.body,
        "",
        'Return a JSON object with exactly these keys: "usage_hint" (one of "reference", '
        '"caveat", "policy", "definition"), "topic_refs" (list of candidate term/entity '
        'names the document discusses, empty if none are clear), and "reasoning" '
        "(one sentence explaining the classification).",
    ]
    return "\n".join(lines)


class RuntimeExtractionSkill:
    """A real ``ExtractionSkill`` backed by the generation runtime (SPEC-E3 §5 amendment).

    For prose sources with no structured classification fields of their own, this is the
    "write once, reuse by every source" extraction step the amendment describes. A
    malformed or empty model response falls back to the same reference/no-topics default
    as :class:`~canonic.connectors.evidence.NullExtractionSkill` rather than failing the
    whole ingest over one bad classification.
    """

    def __init__(self, runtime: GenerationRuntime) -> None:
        self._runtime = runtime

    async def extract(self, doc: RawDoc, *, source: str) -> DocEvidence:
        completion = await self._runtime.generate(
            _extraction_prompt(doc),
            task=Task.EXTRACT,
            system=_EXTRACTION_SYSTEM,
            response_model=_ExtractionResponse,
        )
        if not completion.parsed:
            return await NullExtractionSkill().extract(doc, source=source)

        usage_hint = parse_usage_hint(completion.parsed.get("usage_hint"), doc.source_ref)
        topic_refs = completion.parsed.get("topic_refs", [])
        fingerprint = compute_doc_fingerprint(doc.title, doc.body, usage_hint.value, topic_refs)
        return DocEvidence(
            source=source,
            title=doc.title,
            body=doc.body,
            topic_refs=topic_refs,
            usage_hint=usage_hint,
            native_ref=doc.source_ref,
            source_fingerprint=fingerprint,
            observed_at=datetime.now(UTC),
        )


def make_extraction_skill(
    llm: LLMConfig | None,
    runtime: RuntimeConfig,
    *,
    headless: bool,
) -> ExtractionSkill:
    """Return the right ExtractionSkill for the operating mode (SPEC-E10 §9 pattern).

    Headless or no LLM configured → :class:`~canonic.connectors.evidence.NullExtractionSkill`
    (zero model calls, deterministic). Interactive with LLM → :class:`RuntimeExtractionSkill`
    backed by ``GenerationRuntime``. Mirrors ``make_drafter``/``make_reconcile_drafter``
    exactly, including threading the air-gapped egress policy.
    """
    if headless or llm is None:
        return NullExtractionSkill()
    from canonic.airgap import EgressPolicy
    from canonic.runtime.generation import GenerationRuntime

    policy: EgressPolicy | None = (
        EgressPolicy(allow_cidrs=runtime.allow_cidrs) if runtime.air_gapped else None
    )
    return RuntimeExtractionSkill(GenerationRuntime(llm, policy=policy))
