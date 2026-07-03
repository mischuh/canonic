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

__all__ = [
    "ExtractionSkill",
    "FetchAdapter",
    "GenericEvidenceConnector",
    "NullExtractionSkill",
    "RawDoc",
]


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
