"""Tests for the generic fetch/extract evidence connector split.

Covers docs/AMENDMENT-generic-evidence-connector.md: RawDoc, FetchAdapter,
ExtractionSkill, GenericEvidenceConnector, and NullExtractionSkill in isolation from
any vendor (Notion has its own tests in test_notion.py).
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from canonic.connectors.base import Capability, DocEvidence, Health, UsageHint
from canonic.connectors.evidence import (
    GenericEvidenceConnector,
    NullExtractionSkill,
    RawDoc,
)


class _FakeFetchAdapter:
    def __init__(self, docs: list[RawDoc], *, fail: Exception | None = None) -> None:
        self._docs = docs
        self._fail = fail

    async def fetch(self) -> list[RawDoc]:
        if self._fail is not None:
            raise self._fail
        return self._docs


class _UppercaseExtractionSkill:
    """Fake skill proving the extraction step is independent of the fetch adapter."""

    def __init__(self) -> None:
        self.calls: list[RawDoc] = []

    async def extract(self, doc: RawDoc, *, source: str) -> DocEvidence:
        self.calls.append(doc)
        return DocEvidence(
            source=source,
            title=doc.title.upper(),
            body=doc.body,
            usage_hint=UsageHint.POLICY,
            topic_refs=["from_metadata"] if doc.metadata else [],
            native_ref=doc.source_ref,
            observed_at=datetime.now(UTC),
        )


class TestRawDoc:
    def test_frozen(self) -> None:
        doc = RawDoc(source_ref="x:1", title="t", body="b")
        with pytest.raises(ValidationError):
            doc.title = "other"  # type: ignore[misc]

    def test_metadata_defaults_empty(self) -> None:
        doc = RawDoc(source_ref="x:1", title="t", body="b")
        assert doc.metadata == {}

    def test_metadata_passthrough(self) -> None:
        doc = RawDoc(source_ref="x:1", title="t", body="b", metadata={"author": "jane"})
        assert doc.metadata == {"author": "jane"}


class TestGenericEvidenceConnectorCapabilities:
    def test_declares_extract_evidence_and_test_connection_only(self) -> None:
        connector = GenericEvidenceConnector(_FakeFetchAdapter([]), source="fake")
        caps = set(connector.capabilities())
        assert caps == {
            Capability.CAPABILITIES,
            Capability.TEST_CONNECTION,
            Capability.EXTRACT_EVIDENCE,
        }


class TestGenericEvidenceConnectorTestConnection:
    async def test_ok_when_fetch_succeeds(self) -> None:
        connector = GenericEvidenceConnector(_FakeFetchAdapter([]), source="fake")
        health = await connector.test_connection()
        assert health == Health(status="ok", message="fake: reachable")

    async def test_error_when_fetch_raises(self) -> None:
        connector = GenericEvidenceConnector(
            _FakeFetchAdapter([], fail=RuntimeError("boom")), source="fake"
        )
        health = await connector.test_connection()
        assert health.status == "error"
        assert "boom" in (health.message or "")


class TestGenericEvidenceConnectorExtractEvidence:
    async def test_dispatches_each_raw_doc_through_extraction_skill(self) -> None:
        docs = [
            RawDoc(source_ref="fake:1", title="one", body="body one", metadata={"k": "v"}),
            RawDoc(source_ref="fake:2", title="two", body="body two"),
        ]
        skill = _UppercaseExtractionSkill()
        connector = GenericEvidenceConnector(
            _FakeFetchAdapter(docs), source="fake", extraction_skill=skill
        )

        evidence = await connector.extract_evidence()

        assert len(skill.calls) == 2
        assert [e.title for e in evidence] == ["ONE", "TWO"]
        assert evidence[0].topic_refs == ["from_metadata"]
        assert evidence[1].topic_refs == []
        assert all(e.source == "fake" for e in evidence)

    async def test_propagates_fetch_failure_uncaught(self) -> None:
        connector = GenericEvidenceConnector(
            _FakeFetchAdapter([], fail=RuntimeError("unsupported version")), source="fake"
        )
        with pytest.raises(RuntimeError, match="unsupported version"):
            await connector.extract_evidence()

    async def test_defaults_to_null_extraction_skill(self) -> None:
        docs = [RawDoc(source_ref="fake:1", title="one", body="body one")]
        connector = GenericEvidenceConnector(_FakeFetchAdapter(docs), source="fake")

        evidence = await connector.extract_evidence()

        assert len(evidence) == 1
        assert evidence[0].usage_hint == UsageHint.REFERENCE
        assert evidence[0].topic_refs == []
        assert evidence[0].source_fingerprint is None


class TestNullExtractionSkill:
    async def test_classifies_as_reference_with_no_topics(self) -> None:
        skill = NullExtractionSkill()
        doc = RawDoc(source_ref="fake:1", title="t", body="b", metadata={"any": "thing"})

        evidence = await skill.extract(doc, source="fake")

        assert evidence.usage_hint == UsageHint.REFERENCE
        assert evidence.topic_refs == []
        assert evidence.native_ref == "fake:1"
        assert evidence.source == "fake"
