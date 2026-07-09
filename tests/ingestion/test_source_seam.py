"""Tests for the normalized E3→E4 seam (SPEC-E3 §9 S7).

AC1 — E4 receives only definition/usage_evidence/doc_evidence/relation_schema items.
AC2 — Unknown evidence kind is recorded (logged) and skipped, never guessed or crashed.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from canonic.connectors.base import (
    AcquisitionTier,
    ColumnInfo,
    DefinitionEntityType,
    DefinitionEvidence,
    DefinitionExtract,
    DocEvidence,
    ForeignKey,
    RelationSchema,
    UsageDefinition,
    UsageEvidence,
    UsageHint,
    UsageRole,
    compute_fingerprint,
)

if TYPE_CHECKING:
    import pytest
from canonic.ingestion.builder import ContextBuilder, SkippedEvidence
from canonic.ingestion.models import EvidenceItem, EvidenceKind
from canonic.ingestion.source import evidence_from_definitions, evidence_from_docs

_NOW = datetime(2026, 6, 19, 12, 0, 0, tzinfo=UTC)
_SOURCE = "test_conn"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _columns() -> list[ColumnInfo]:
    return [ColumnInfo(name="id", type="int", nullable=False, position=1)]


def _relation_schema() -> RelationSchema:
    cols = _columns()
    pk = ["id"]
    fks: list[ForeignKey] = []
    return RelationSchema(
        connection=_SOURCE,
        relation="analytics.dim_accounts",
        kind="table",
        columns=cols,
        primary_key=pk,
        foreign_keys=fks,
        acquisition_tier=AcquisitionTier.MODELING,
        source_fingerprint=compute_fingerprint(cols, pk, fks),
    )


def _doc_evidence() -> DocEvidence:
    return DocEvidence(
        source=_SOURCE,
        title="Revenue definition",
        body="Revenue is net of refunds.",
        usage_hint=UsageHint.DEFINITION,
        native_ref="notion:page:abc123",
        observed_at=_NOW,
    )


def _usage_evidence() -> UsageEvidence:
    return UsageEvidence(
        source=_SOURCE,
        artifact="question:7",
        title="Monthly revenue",
        defines=UsageDefinition(expr="SUM(amount)", references=["fct_orders"]),
        role=UsageRole.TRUSTED_EXAMPLE,
        native_ref="metabase:question:7",
        observed_at=_NOW,
    )


def _definition_evidence() -> DefinitionEvidence:
    return DefinitionEvidence(
        source=_SOURCE,
        entity="revenue",
        entity_type=DefinitionEntityType.MEASURE,
        expr="SUM(amount)",
        native_ref="model.project.revenue",
        acquisition_tier=AcquisitionTier.MODELING,
    )


class FakeEvidenceExtractable:
    """Returns a fixed list from extract_evidence(); controllable for each test."""

    def __init__(self, items: list[Any]) -> None:
        self._items = items

    async def extract_evidence(self) -> list[Any]:
        return list(self._items)


class FakeDefinitionExtractable:
    """Returns a fixed DefinitionExtract from extract_definitions()."""

    def __init__(self, extract: DefinitionExtract) -> None:
        self._extract = extract

    async def extract_definitions(self) -> DefinitionExtract:
        return self._extract


# ---------------------------------------------------------------------------
# AC1 — only normalized kinds reach E4
# ---------------------------------------------------------------------------


class TestAC1NormalizedKindsOnly:
    async def test_doc_evidence_yields_doc_evidence_kind(self) -> None:
        connector = FakeEvidenceExtractable([_doc_evidence()])
        items = await evidence_from_docs(connector, _SOURCE)

        assert len(items) == 1
        assert items[0].kind == EvidenceKind.DOC_EVIDENCE

    async def test_usage_evidence_yields_usage_evidence_kind(self) -> None:
        connector = FakeEvidenceExtractable([_usage_evidence()])
        items = await evidence_from_docs(connector, _SOURCE)

        assert len(items) == 1
        assert items[0].kind == EvidenceKind.USAGE_EVIDENCE

    async def test_payload_re_validates_as_doc_evidence(self) -> None:
        connector = FakeEvidenceExtractable([_doc_evidence()])
        items = await evidence_from_docs(connector, _SOURCE)

        DocEvidence.model_validate(items[0].payload)

    async def test_payload_re_validates_as_usage_evidence(self) -> None:
        connector = FakeEvidenceExtractable([_usage_evidence()])
        items = await evidence_from_docs(connector, _SOURCE)

        UsageEvidence.model_validate(items[0].payload)

    async def test_definition_evidence_yields_definition_kind(self) -> None:
        extract = DefinitionExtract(
            relations=[_relation_schema()], definitions=[_definition_evidence()]
        )
        connector = FakeDefinitionExtractable(extract)
        items = await evidence_from_definitions(connector, _SOURCE)

        kinds = {item.kind for item in items}
        assert kinds == {EvidenceKind.RELATION_SCHEMA, EvidenceKind.DEFINITION}

    async def test_no_vendor_keys_in_doc_payload(self) -> None:
        connector = FakeEvidenceExtractable([_doc_evidence()])
        items = await evidence_from_docs(connector, _SOURCE)

        normalized_keys = set(DocEvidence.model_fields)
        extra = set(items[0].payload) - normalized_keys
        assert not extra, f"Vendor keys leaked into payload: {extra}"

    async def test_no_vendor_keys_in_usage_payload(self) -> None:
        connector = FakeEvidenceExtractable([_usage_evidence()])
        items = await evidence_from_docs(connector, _SOURCE)

        normalized_keys = set(UsageEvidence.model_fields)
        extra = set(items[0].payload) - normalized_keys
        assert not extra, f"Vendor keys leaked into payload: {extra}"


# ---------------------------------------------------------------------------
# AC2 — unknown evidence type: logged and skipped, never guessed or crashed
# ---------------------------------------------------------------------------


class VendorSpecificBlob:
    """Simulates a connector returning a raw vendor shape instead of a normalized type."""

    native_ref = "vendor:blob:xyz"


class TestAC2UnknownKindRecordedAndSkipped:
    async def test_unknown_type_excluded_from_result(self) -> None:
        connector = FakeEvidenceExtractable([VendorSpecificBlob()])
        items = await evidence_from_docs(connector, _SOURCE)

        assert items == []

    async def test_unknown_type_does_not_crash(self) -> None:
        connector = FakeEvidenceExtractable([VendorSpecificBlob(), _doc_evidence()])
        items = await evidence_from_docs(connector, _SOURCE)

        assert len(items) == 1
        assert items[0].kind == EvidenceKind.DOC_EVIDENCE

    async def test_unknown_type_logged_as_error(self, caplog: pytest.LogCaptureFixture) -> None:
        connector = FakeEvidenceExtractable([VendorSpecificBlob()])
        with caplog.at_level(logging.ERROR, logger="canonic.ingestion.source"):
            await evidence_from_docs(connector, _SOURCE)

        assert any("unknown evidence kind" in r.message for r in caplog.records)

    async def test_log_contains_source(self, caplog: pytest.LogCaptureFixture) -> None:
        connector = FakeEvidenceExtractable([VendorSpecificBlob()])
        with caplog.at_level(logging.ERROR, logger="canonic.ingestion.source"):
            await evidence_from_docs(connector, "my_source")

        assert any("my_source" in r.message for r in caplog.records)

    async def test_log_contains_native_ref(self, caplog: pytest.LogCaptureFixture) -> None:
        connector = FakeEvidenceExtractable([VendorSpecificBlob()])
        with caplog.at_level(logging.ERROR, logger="canonic.ingestion.source"):
            await evidence_from_docs(connector, _SOURCE)

        assert any("vendor:blob:xyz" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# AC2 — schema-invalid item from connector: logged and dropped
# ---------------------------------------------------------------------------


class TestAC2SchemaInvalidDropped:
    async def test_schema_invalid_doc_evidence_logged(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        invalid = DocEvidence.model_construct(
            source=_SOURCE,
            title=None,  # required field missing
            body="x",
            usage_hint=UsageHint.DEFINITION,
            native_ref="notion:page:bad",
            observed_at=_NOW,
        )
        connector = FakeEvidenceExtractable([invalid])
        with caplog.at_level(logging.ERROR, logger="canonic.ingestion.source"):
            items = await evidence_from_docs(connector, _SOURCE)

        assert items == []
        assert any("notion:page:bad" in r.message for r in caplog.records)

    async def test_schema_invalid_usage_evidence_logged(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        invalid = UsageEvidence.model_construct(
            source=_SOURCE,
            artifact=None,  # required field missing
            title="x",
            defines=None,  # required — will fail validation
            role=UsageRole.TRUSTED_EXAMPLE,
            native_ref="metabase:question:bad",
            observed_at=_NOW,
        )
        connector = FakeEvidenceExtractable([invalid])
        with caplog.at_level(logging.ERROR, logger="canonic.ingestion.source"):
            items = await evidence_from_docs(connector, _SOURCE)

        assert items == []
        assert any("metabase:question:bad" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# Regression — builder open-kind still works (EvidenceItem with future kind)
# ---------------------------------------------------------------------------


class TestOpenKindPreserved:
    async def test_future_kind_lands_in_builder_skip_ledger(self) -> None:
        future_item = EvidenceItem(
            source=_SOURCE,
            kind="future_kind",
            acquisition_tier=AcquisitionTier.LIVE,
            payload={"foo": "bar"},
            source_fingerprint="sha256:abc",
            observed_at=_NOW,
        )
        result = await ContextBuilder().build([future_item])

        assert result.proposals == []
        assert result.skipped == [
            SkippedEvidence(source=_SOURCE, kind="future_kind", reason="unknown evidence kind")
        ]


# ---------------------------------------------------------------------------
# S8 (GH-94) — BI SQL is parsed as a definition candidate, never executed (AC1)
# ---------------------------------------------------------------------------


class TestS8NoExecutionPath:
    """BI question SQL surfaces as UsageEvidence metadata; it is never forwarded to a warehouse.

    The EvidenceExtractable protocol has no ``run_read_only_sql`` or ``introspect_schema``
    method, so the SQL string can only leave via the evidence metadata path (S8-AC1).
    """

    async def test_bi_sql_preserved_in_usage_evidence_expr(self) -> None:
        """SQL from a BI question is stored in UsageEvidence.defines.expr, not executed."""
        bi_sql = "SELECT SUM(amount) FROM fct_orders WHERE status = 'complete'"
        evidence = UsageEvidence(
            source=_SOURCE,
            artifact="question:42",
            title="Completed revenue",
            defines=UsageDefinition(expr=bi_sql, references=["fct_orders"]),
            role=UsageRole.TRUSTED_EXAMPLE,
            native_ref="metabase:question:42",
            observed_at=_NOW,
        )
        connector = FakeEvidenceExtractable([evidence])
        items = await evidence_from_docs(connector, _SOURCE)

        assert len(items) == 1
        assert items[0].kind == EvidenceKind.USAGE_EVIDENCE
        payload = UsageEvidence.model_validate(items[0].payload)
        assert payload.defines.expr == bi_sql

    async def test_evidence_extractor_has_no_run_sql_method(self) -> None:
        """FakeEvidenceExtractable (and EvidenceExtractable protocol) exposes no SQL executor."""
        connector = FakeEvidenceExtractable([])
        assert not hasattr(type(connector), "run_read_only_sql"), (
            "EvidenceExtractable must not define run_read_only_sql — E3 no-execution boundary (S8)"
        )
        assert not hasattr(type(connector), "introspect_schema"), (
            "EvidenceExtractable must not define introspect_schema — E3 no-execution boundary (S8)"
        )

    async def test_definition_evidence_sql_preserved_not_executed(self) -> None:
        """SQL in DefinitionEvidence.expr crosses the seam as metadata only."""
        defn_sql = "SUM(amount)"
        evidence = DefinitionEvidence(
            source=_SOURCE,
            entity="revenue",
            entity_type=DefinitionEntityType.MEASURE,
            expr=defn_sql,
            native_ref="model.project.revenue",
            acquisition_tier=AcquisitionTier.MODELING,
        )
        connector = FakeDefinitionExtractable(
            DefinitionExtract(definitions=[evidence], relations=[])
        )
        items = await evidence_from_definitions(connector, _SOURCE)

        defn_items = [i for i in items if i.kind == EvidenceKind.DEFINITION]
        assert len(defn_items) == 1
        payload = DefinitionEvidence.model_validate(defn_items[0].payload)
        assert payload.expr == defn_sql
