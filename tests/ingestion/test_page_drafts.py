"""Knowledge pages drafted from doc evidence during ingest."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from canonic.config import ReconcileConfig, scaffold_project
from canonic.connectors.base import (
    AcquisitionTier,
    Capability,
    ColumnInfo,
    ConnectorBase,
    DocEvidence,
    Health,
    RelationSchema,
    UsageHint,
    compute_fingerprint,
)
from canonic.connectors.evidence import compute_doc_fingerprint
from canonic.ingestion.builder import MODELING_REVIEW_CONFIDENCE, ContextBuilder
from canonic.ingestion.models import EvidenceItem, EvidenceKind, ReconciliationDecision
from canonic.ingestion.pipeline import IngestionPipeline, write_emitted_diffs
from canonic.ingestion.source import evidence_from_introspection
from canonic.knowledge.loader import dump_knowledge_page, load_knowledge_page
from canonic.knowledge.models import UsageMode
from canonic.knowledge.validation import EntityIndex, PageIndex, ReferenceValidator
from canonic.semantic.loader import list_semantic_sources
from canonic.semantic.models import Provenance

if TYPE_CHECKING:
    from pathlib import Path

_NOW = datetime(2026, 10, 5, 12, 0, 0, tzinfo=UTC)
_CONN = "warehouse"
_COLUMNS = [
    ColumnInfo(name="order_id", type="int", nullable=False),
    ColumnInfo(name="status", type="string"),
    ColumnInfo(name="amount", type="decimal"),
]
_ORDERS = RelationSchema(
    connection=_CONN,
    relation="main.orders",
    kind="table",
    columns=_COLUMNS,
    primary_key=["order_id"],
    acquisition_tier=AcquisitionTier.LIVE,
    source_fingerprint=compute_fingerprint(_COLUMNS, ["order_id"], []),
)
_PAGE = "knowledge/global/shop-orders-status.md"


class _Live(ConnectorBase):
    def capabilities(self) -> list[Capability]:
        return [Capability.INTROSPECT_SCHEMA, Capability.TEST_CONNECTION]

    async def test_connection(self) -> Health:
        return Health(status="ok")

    async def introspect_schema(self) -> list[RelationSchema]:
        return [_ORDERS]


def _doc(
    body: str = "Valid values are placed, shipped and cancelled.",
    *,
    title: str = "shop: orders.status",
    topic_refs: list[str] | None = None,
    usage_hint: UsageHint = UsageHint.CAVEAT,
) -> EvidenceItem:
    refs = topic_refs if topic_refs is not None else [f"{_CONN}.orders.status", "status", "nope"]
    doc = DocEvidence(
        source=_CONN,
        title=title,
        body=body,
        topic_refs=refs,
        usage_hint=usage_hint,
        native_ref="ossie:shop/orders/status",
        source_fingerprint=compute_doc_fingerprint(title, body, usage_hint.value, refs),
        observed_at=_NOW,
    )
    return EvidenceItem(
        source=_CONN,
        kind=EvidenceKind.DOC_EVIDENCE,
        acquisition_tier=AcquisitionTier.HAND_AUTHORED,
        payload=doc.model_dump(mode="json"),
        source_fingerprint=doc.source_fingerprint or "sha256:none",
        observed_at=_NOW,
    )


async def _run(root: Path, *docs: EvidenceItem, apply: bool = True) -> Any:
    live = _Live()
    pipeline = IngestionPipeline(root, {_CONN: live}, ReconcileConfig())
    evidence = await evidence_from_introspection(live, _CONN) + list(docs)
    result = await pipeline.run(evidence)
    if apply:
        write_emitted_diffs(root, result.emission.diffs)
    return result


def _entry(result: Any, target: str = _PAGE) -> Any:
    return next(e for e in result.report.entries if e.target == target)


async def test_doc_evidence_becomes_a_page_draft_for_review() -> None:
    result = await ContextBuilder().build([_doc()])
    (draft,) = result.proposals
    assert draft.target == _PAGE
    assert draft.provenance is Provenance.INFERRED
    assert draft.confidence == MODELING_REVIEW_CONFIDENCE
    assert result.skipped == []


async def test_invalid_doc_payload_is_skipped() -> None:
    item = _doc().model_copy(update={"payload": {"title": "x"}})
    result = await ContextBuilder().build([item])
    assert result.proposals == []
    (skip,) = result.skipped
    assert skip.reason.startswith("invalid doc evidence payload")


async def test_page_resolves_references_against_the_run(tmp_path: Path) -> None:
    scaffold_project(tmp_path)
    result = await _run(tmp_path, _doc())

    entry = _entry(result)
    assert entry.decision is ReconciliationDecision.ADD
    assert entry.auto_apply is False
    assert entry.proposal.content["meta"]["unresolved_topic_refs"] == ["nope"]

    page = load_knowledge_page(tmp_path / _PAGE)
    assert page.sl_refs == [f"{_CONN}.orders.status"]
    assert page.usage_mode is UsageMode.CAVEAT
    assert page.meta.provenance is Provenance.INFERRED
    assert page.meta.source_fingerprint
    assert page.body == "Valid values are placed, shipped and cancelled.\n"
    entities = EntityIndex.from_sources(list_semantic_sources(tmp_path))
    ReferenceValidator(entities, PageIndex(slugs_by_scope={})).validate_page(page)


async def test_unchanged_doc_is_a_no_op(tmp_path: Path) -> None:
    scaffold_project(tmp_path)
    await _run(tmp_path, _doc())
    before = (tmp_path / _PAGE).read_text()

    result = await _run(tmp_path, _doc())

    assert _entry(result).decision is ReconciliationDecision.NO_OP
    assert (tmp_path / _PAGE).read_text() == before


async def test_changed_doc_is_an_edit(tmp_path: Path) -> None:
    scaffold_project(tmp_path)
    await _run(tmp_path, _doc())

    result = await _run(tmp_path, _doc("Valid values are placed and shipped."), apply=False)

    assert _entry(result).decision is ReconciliationDecision.EDIT


async def test_changed_doc_against_a_curated_page_is_a_contradiction(tmp_path: Path) -> None:
    scaffold_project(tmp_path)
    await _run(tmp_path, _doc())
    page = load_knowledge_page(tmp_path / _PAGE)
    curated = page.model_copy(
        update={"meta": page.meta.model_copy(update={"provenance": Provenance.HUMAN_CURATED})}
    )
    (tmp_path / _PAGE).write_text(dump_knowledge_page(curated))

    result = await _run(tmp_path, _doc("Valid values are placed and shipped."), apply=False)

    assert _entry(result).decision is ReconciliationDecision.CONTRADICTION


async def test_pages_not_drafted_by_ingest_are_never_pruned(tmp_path: Path) -> None:
    scaffold_project(tmp_path)
    hand_written = tmp_path / "knowledge" / "global" / "refunds.md"
    hand_written.parent.mkdir(parents=True, exist_ok=True)
    hand_written.write_text("---\nsummary: Refund policy\n---\nRefunds take 14 days.\n")

    result = await _run(tmp_path, apply=False)

    assert all(not e.target.startswith("knowledge/") for e in result.report.entries)


async def test_drafted_page_whose_evidence_disappeared_is_proposed_for_prune(
    tmp_path: Path,
) -> None:
    scaffold_project(tmp_path)
    await _run(tmp_path, _doc())

    result = await _run(tmp_path, apply=False)

    assert _entry(result).decision is ReconciliationDecision.PRUNE
