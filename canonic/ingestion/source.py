"""Evidence sources — adapt connector output into the normalized evidence stream (SPEC-E4 §3).

The pipeline consumes :class:`EvidenceItem` only; this module is the seam that turns a
connector's live introspection (E2) or definition extraction (E3) into that stream, keeping
every vendor shape out of the engine. Both the full ingest and the fast bootstrap
(SPEC-E4 §8) gather evidence through here.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from pydantic import ValidationError

from canonic.connectors.base import (
    AcquisitionTier,
    Capability,
    DefinitionEvidence,
    DefinitionExtractable,
    DocEvidence,
    EvidenceExtractable,
    RelationSchema,
    SchemaIntrospectable,
    UsageEvidence,
)
from canonic.ingestion.models import EvidenceItem, EvidenceKind

if TYPE_CHECKING:
    from canonic.connectors.base import ConnectorBase

logger = logging.getLogger(__name__)

__all__ = [
    "evidence_from_definitions",
    "evidence_from_docs",
    "evidence_from_introspection",
    "gather_evidence",
]


def _revalidate[M](item: Any, model_cls: type[M], source: str) -> M | None:
    """Re-validate a connector item against its normalized model before it crosses into E4.

    Returns the validated model or None on failure. On failure logs an error with
    source and native_ref so the drop is always recorded (SPEC-E3 §9 S7-AC1).
    """
    try:
        return model_cls.model_validate(item.model_dump())  # type: ignore[attr-defined,no-any-return]
    except (ValidationError, AttributeError) as exc:
        logger.error(
            "dropping schema-invalid %s from source %r (native_ref=%s): %s",
            model_cls.__name__,
            source,
            getattr(item, "native_ref", "?"),
            exc,
        )
        return None


async def evidence_from_introspection(
    connector: SchemaIntrospectable, source: str
) -> list[EvidenceItem]:
    """Introspect ``connector`` and wrap each relation as a ``relation_schema`` evidence item.

    Tier-1 live introspection (SPEC-E2 §4): every discovered :class:`RelationSchema` becomes one
    self-describing :class:`EvidenceItem` carrying its acquisition tier and source fingerprint,
    so the builder can map it deterministically and reconciliation can detect drift (§7). The
    payload is the schema's JSON dump — no vendor-specific shape crosses the boundary.
    """
    observed_at = datetime.now(UTC)
    schemas = await connector.introspect_schema()
    result: list[EvidenceItem] = []
    for schema in schemas:
        validated = _revalidate(schema, RelationSchema, source)
        if validated is None:
            continue
        result.append(
            EvidenceItem(
                source=source,
                kind=EvidenceKind.RELATION_SCHEMA,
                acquisition_tier=validated.acquisition_tier,
                payload=validated.model_dump(mode="json"),
                source_fingerprint=validated.source_fingerprint or "",
                observed_at=observed_at,
            )
        )
    return result


async def evidence_from_definitions(
    connector: DefinitionExtractable, source: str
) -> list[EvidenceItem]:
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
        validated_schema = _revalidate(schema, RelationSchema, source)
        if validated_schema is None:
            continue
        items.append(
            EvidenceItem(
                source=source,
                kind=EvidenceKind.RELATION_SCHEMA,
                acquisition_tier=validated_schema.acquisition_tier,
                payload=validated_schema.model_dump(mode="json"),
                source_fingerprint=validated_schema.source_fingerprint or "",
                observed_at=observed_at,
            )
        )
    for defn in extract.definitions:
        validated_defn = _revalidate(defn, DefinitionEvidence, source)
        if validated_defn is None:
            continue
        items.append(
            EvidenceItem(
                source=source,
                kind=EvidenceKind.DEFINITION,
                acquisition_tier=AcquisitionTier.MODELING,
                payload=validated_defn.model_dump(mode="json"),
                source_fingerprint=validated_defn.source_fingerprint or "",
                observed_at=observed_at,
            )
        )
    return items


async def evidence_from_docs(connector: EvidenceExtractable, source: str) -> list[EvidenceItem]:
    """Extract evidence from ``connector`` and wrap each as a normalized evidence item.

    The E3 evidence seam (SPEC-E3 §5, §8): dispatches on the concrete evidence type.
    :class:`DocEvidence` → ``doc_evidence`` at ``hand_authored`` tier (Notion, prose).
    :class:`UsageEvidence` → ``usage_evidence`` at ``query_history`` tier (Metabase,
    Looker) — it is observed BI usage, a reconciliation signal, not hand-authored prose.
    No vendor-specific shape crosses into E4.
    """
    items = await connector.extract_evidence()
    result: list[EvidenceItem] = []
    for item in items:
        if isinstance(item, UsageEvidence):
            validated = _revalidate(item, UsageEvidence, source)
            if validated is None:
                continue
            result.append(
                EvidenceItem(
                    source=source,
                    kind=EvidenceKind.USAGE_EVIDENCE,
                    acquisition_tier=AcquisitionTier.QUERY_HISTORY,
                    payload=validated.model_dump(mode="json"),
                    source_fingerprint=validated.source_fingerprint or "",
                    observed_at=validated.observed_at,
                )
            )
        elif isinstance(item, DocEvidence):
            validated_doc = _revalidate(item, DocEvidence, source)
            if validated_doc is None:
                continue
            result.append(
                EvidenceItem(
                    source=source,
                    kind=EvidenceKind.DOC_EVIDENCE,
                    acquisition_tier=AcquisitionTier.HAND_AUTHORED,
                    payload=validated_doc.model_dump(mode="json"),
                    source_fingerprint=validated_doc.source_fingerprint or "",
                    observed_at=validated_doc.observed_at,
                )
            )
        else:
            logger.error(
                "dropping unknown evidence kind from source %r: type=%s native_ref=%s",
                source,
                type(item).__name__,
                getattr(item, "native_ref", "?"),
            )
    return result


_EvidenceSeam = Callable[[Any, str], Awaitable[list[EvidenceItem]]]

_SEAM_BY_CAPABILITY: dict[Capability, _EvidenceSeam] = {
    Capability.INTROSPECT_SCHEMA: evidence_from_introspection,
    Capability.EXTRACT_DEFINITIONS: evidence_from_definitions,
    Capability.EXTRACT_EVIDENCE: evidence_from_docs,
}


async def gather_evidence(connector: ConnectorBase, source: str) -> list[EvidenceItem]:
    """Dispatch on declared capabilities and invoke the matching evidence seam for each.

    The core evidence-gathering entry point (SPEC-E3 §2, S4): iterates
    ``connector.capabilities()`` and invokes the seam mapped to each evidence-producing
    capability.  A connector declaring both ``extract_definitions`` and ``extract_evidence``
    has both seams called — multi-capability, zero vendor-name branches.
    Capabilities with no mapped seam (e.g. ``test_connection``) are silently skipped.
    """
    items: list[EvidenceItem] = []
    for cap in connector.capabilities():
        seam = _SEAM_BY_CAPABILITY.get(cap)
        if seam is not None:
            items.extend(await seam(connector, source))
    return items
