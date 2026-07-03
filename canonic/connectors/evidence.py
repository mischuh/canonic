"""Generic evidence connector — fetch/extract split for prose evidence sources.

Splits an evidence connector into a thin, vendor-specific :class:`FetchAdapter` (auth,
pagination, native API shape — no classification judgment) and one shared
:class:`GenericEvidenceConnector` that turns each :class:`RawDoc` into normalized
:class:`~canonic.connectors.base.DocEvidence` via a pluggable :class:`ExtractionSkill`.
Adding a new prose source (Confluence, Google Docs, ...) is: write a ``FetchAdapter``,
register it with the connector factory — no new extraction or ``DocEvidence``-mapping
code (docs/AMENDMENT-generic-evidence-connector.md).

Definition connectors (dbt, LookML/MetricFlow) are out of scope: their extraction stays
deterministic, structured parsing and is not built from these pieces.
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import UTC, datetime
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict

from canonic.connectors.base import (
    Capability,
    ConnectorBase,
    DocEvidence,
    Health,
    UsageEvidence,
    UsageHint,
)

logger = logging.getLogger(__name__)

__all__ = [
    "ExtractionSkill",
    "FetchAdapter",
    "GenericEvidenceConnector",
    "NullExtractionSkill",
    "RawDoc",
    "compute_doc_fingerprint",
    "parse_usage_hint",
]

# Map from a raw classification string (case-insensitive) → UsageHint. Shared by every
# ExtractionSkill — deterministic property-readers and LLM-backed skills alike — so an
# unfamiliar vendor value or a stray model classification degrades identically everywhere.
_USAGE_HINT_MAP: dict[str, UsageHint] = {
    "reference": UsageHint.REFERENCE,
    "caveat": UsageHint.CAVEAT,
    "policy": UsageHint.POLICY,
    "definition": UsageHint.DEFINITION,
}


def parse_usage_hint(raw: str | None, context: str) -> UsageHint:
    """Map a raw classification string to UsageHint, defaulting gracefully.

    Defaults to ``REFERENCE`` and logs a WARNING for unrecognized values — never drops
    the doc (SPEC-E3 §4 AC2-style graceful handling). ``context`` identifies the doc in
    the warning (e.g. a page id or source_ref) and carries no other meaning.
    """
    if not raw:
        return UsageHint.REFERENCE
    mapped = _USAGE_HINT_MAP.get(raw.strip().lower())
    if mapped is None:
        logger.warning("unrecognized usage_hint %r for %s; recorded as reference", raw, context)
        return UsageHint.REFERENCE
    return mapped


def compute_doc_fingerprint(title: str, body: str, usage_hint: str, topic_refs: list[str]) -> str:
    """Stable sha256 over doc content fields, for drift detection (SPEC-E3 §3.2).

    Shared by every ExtractionSkill so the same content fingerprints identically
    regardless of which skill (deterministic or LLM-backed) produced the classification.
    """
    payload = {
        "title": title,
        "body": body,
        "usage_hint": usage_hint,
        "topic_refs": sorted(topic_refs),
    }
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
    return f"sha256:{digest}"


class RawDoc(BaseModel):
    """Raw fetched content before extraction — a :class:`FetchAdapter`'s sole output shape.

    No mapping to ``DocEvidence`` and no ``usage_hint``/``topic_refs`` judgment happens
    here; that is the :class:`ExtractionSkill`'s job. ``metadata`` passes native vendor
    fields through as-is (author, space, last_edited, structured properties, ...) so an
    extraction skill can use them when a source exposes more than plain text.
    """

    model_config = ConfigDict(frozen=True)

    source_ref: str
    title: str
    body: str
    metadata: dict[str, Any] = {}


@runtime_checkable
class FetchAdapter(Protocol):
    """Vendor-specific fetch seam: owns auth and pagination only, no extraction."""

    async def fetch(self) -> list[RawDoc]:
        """Retrieve raw content. No extraction, no classification — just I/O."""
        ...


class ExtractionSkill(Protocol):
    """Turns one ``RawDoc`` into normalized ``DocEvidence`` — written once, reused by every source."""

    async def extract(self, doc: RawDoc, *, source: str) -> DocEvidence:
        """Classify ``doc`` into normalized evidence, stamped with the given ``source`` id."""
        ...


class NullExtractionSkill:
    """Extraction stub that classifies nothing (safe default, mirrors ``NullLLMDrafter``).

    Every doc is recorded as ``usage_hint=reference`` with no ``topic_refs`` — it never
    fabricates a classification it has no basis for. Callers that need real
    classification inject a source-aware :class:`ExtractionSkill` instead.
    """

    async def extract(self, doc: RawDoc, *, source: str) -> DocEvidence:
        return DocEvidence(
            source=source,
            title=doc.title,
            body=doc.body,
            usage_hint=UsageHint.REFERENCE,
            native_ref=doc.source_ref,
            observed_at=datetime.now(UTC),
        )


class GenericEvidenceConnector(ConnectorBase):
    """Evidence connector composed from a ``FetchAdapter`` + ``ExtractionSkill`` (E3 §5 amendment).

    Fetching (auth, pagination, native API shape) and extraction (usage_hint/topic_refs
    classification) are independent concerns; this class owns only the wiring between
    them and satisfies the E3 §2 ``extract_evidence()`` capability contract. A vendor
    adds a source by writing a ``FetchAdapter`` and registering it with the connector
    factory — no new extraction or ``DocEvidence``-mapping code.

    Args:
        fetch_adapter: Vendor-specific raw content source.
        source: Connection id used to stamp emitted evidence items.
        extraction_skill: Turns each ``RawDoc`` into ``DocEvidence``. Defaults to
            :class:`NullExtractionSkill` (classifies nothing) when not given.
    """

    def __init__(
        self,
        fetch_adapter: FetchAdapter,
        *,
        source: str,
        extraction_skill: ExtractionSkill | None = None,
    ) -> None:
        self._fetch_adapter = fetch_adapter
        self._source = source
        self._extraction_skill = extraction_skill or NullExtractionSkill()

    @property
    def extraction_skill(self) -> ExtractionSkill:
        """The currently active extraction skill (read-only; use ``set_extraction_skill`` to replace)."""
        return self._extraction_skill

    def set_extraction_skill(self, extraction_skill: ExtractionSkill) -> None:
        """Replace the extraction skill after construction.

        The connector factory only threads a bare ``Connection`` into connector
        builders (SPEC-E2 §2.2a) — it has no access to ``LLMConfig`` — so a real,
        LLM-backed skill (:class:`~canonic.runtime.extraction.RuntimeExtractionSkill`)
        is built separately from ``config.llm``/``config.runtime`` and wired in here by
        the caller, exactly like ``LLMDrafter`` is wired into ``ContextBuilder`` rather
        than into a connector at factory-build time.
        """
        self._extraction_skill = extraction_skill

    def capabilities(self) -> list[Capability]:
        return [Capability.CAPABILITIES, Capability.TEST_CONNECTION, Capability.EXTRACT_EVIDENCE]

    async def test_connection(self) -> Health:
        """Probe the fetch adapter; any failure (auth, network, unsupported version) is Health(error)."""
        try:
            await self._fetch_adapter.fetch()
        except Exception as exc:  # noqa: BLE001 — connector-boundary translation to Health
            return Health(status="error", message=str(exc))
        return Health(status="ok", message=f"{self._source}: reachable")

    async def extract_evidence(self) -> list[DocEvidence | UsageEvidence]:
        """Fetch raw docs and run each through the extraction skill (E3 §5 amendment).

        Propagates a fetch-adapter failure (e.g. :exc:`~canonic.exc.UnsupportedSourceVersionError`)
        uncaught so no partial ingest occurs (SPEC-E3 §6, PRD FR-2).
        """
        raw_docs = await self._fetch_adapter.fetch()
        return [await self._extraction_skill.extract(doc, source=self._source) for doc in raw_docs]
