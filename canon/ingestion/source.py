"""Evidence sources — adapt connector output into the normalized evidence stream (SPEC-E4 §3).

The pipeline consumes :class:`EvidenceItem` only; this module is the seam that turns a
connector's live introspection (E2) into that stream, keeping every vendor shape out of the
engine. Both the full ingest and the fast bootstrap (SPEC-E4 §8) gather evidence through here.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

from canon.ingestion.models import EvidenceItem, EvidenceKind

if TYPE_CHECKING:
    from canon.connectors.base import ConnectorBase

__all__ = ["evidence_from_introspection"]


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
