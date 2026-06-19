"""Evidence sources — adapt connector output into the normalized evidence stream (SPEC-E4 §3).

The pipeline consumes :class:`EvidenceItem` only; this module is the seam that turns a
connector's live introspection (E2) or definition extraction (E3) into that stream, keeping
every vendor shape out of the engine. Both the full ingest and the fast bootstrap
(SPEC-E4 §8) gather evidence through here.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

from canon.connectors.base import AcquisitionTier
from canon.ingestion.models import EvidenceItem, EvidenceKind

if TYPE_CHECKING:
    from canon.connectors.base import ConnectorBase

__all__ = ["evidence_from_definitions", "evidence_from_docs", "evidence_from_introspection"]


async def evidence_from_introspection(connector: ConnectorBase, source: str) -> list[EvidenceItem]:
    """Introspect ``connector`` and wrap each relation as a ``relation_schema`` evidence item.

    Tier-1 live introspection (SPEC-E2 §4): every discovered :class:`RelationSchema` becomes one
    self-describing :class:`EvidenceItem` carrying its acquisition tier and source fingerprint,
    so the builder can map it deterministically and reconciliation can detect drift (§7). The
    payload is the schema's JSON dump — no vendor-specific shape crosses the boundary.
    """
    observed_at = datetime.now(UTC)
    schemas = await connector.introspect_schema()
    return [
        EvidenceItem(
            source=source,
            kind=EvidenceKind.RELATION_SCHEMA,
            acquisition_tier=schema.acquisition_tier,
            payload=schema.model_dump(mode="json"),
            source_fingerprint=schema.source_fingerprint or "",
            observed_at=observed_at,
        )
        for schema in schemas
    ]


async def evidence_from_definitions(connector: ConnectorBase, source: str) -> list[EvidenceItem]:
    """Extract definitions from ``connector`` and wrap each as a normalized evidence item.

    The E3 seam (SPEC-E3 §2, §8): each :class:`RelationSchema` at modeling tier becomes a
    ``relation_schema`` item and each :class:`DefinitionEvidence` becomes a ``definition``
    item — no vendor-specific shape crosses into E4.  The builder records ``definition``
    items in its skip ledger until the E4 handler is implemented.
    """
    observed_at = datetime.now(UTC)
    extract = await connector.extract_definitions()
    items: list[EvidenceItem] = []
    for schema in extract.relations:
        items.append(
            EvidenceItem(
                source=source,
                kind=EvidenceKind.RELATION_SCHEMA,
                acquisition_tier=schema.acquisition_tier,
                payload=schema.model_dump(mode="json"),
                source_fingerprint=schema.source_fingerprint or "",
                observed_at=observed_at,
            )
        )
    for defn in extract.definitions:
        items.append(
            EvidenceItem(
                source=source,
                kind=EvidenceKind.DEFINITION,
                acquisition_tier=AcquisitionTier.MODELING,
                payload=defn.model_dump(mode="json"),
                source_fingerprint=defn.source_fingerprint or "",
                observed_at=observed_at,
            )
        )
    return items


async def evidence_from_docs(connector: ConnectorBase, source: str) -> list[EvidenceItem]:
    """Extract prose evidence from ``connector`` and wrap each as a normalized evidence item.

    The E3 evidence seam (SPEC-E3 §5, §8): each :class:`DocEvidence` becomes a
    ``doc_evidence`` item at ``hand_authored`` acquisition tier — no vendor-specific
    shape crosses into E4.  ``topic_refs`` are candidates; E6 resolves them on write.
    """
    docs = await connector.extract_evidence()
    return [
        EvidenceItem(
            source=source,
            kind=EvidenceKind.DOC_EVIDENCE,
            acquisition_tier=AcquisitionTier.HAND_AUTHORED,
            payload=doc.model_dump(mode="json"),
            source_fingerprint=doc.source_fingerprint or "",
            observed_at=doc.observed_at,
        )
        for doc in docs
    ]
