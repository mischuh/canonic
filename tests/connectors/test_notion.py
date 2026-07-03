"""Tests for the Notion evidence connector (SPEC-E3 §5, §9 S2 AC1)."""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any

import pytest

from canonic.connectors.base import (
    Capability,
    DocEvidence,
    UsageHint,
)
from canonic.connectors.evidence import GenericEvidenceConnector
from canonic.connectors.notion import (
    SUPPORTED_API_VERSIONS,
    _usage_hint_for,
    make_notion_connector,
)
from canonic.ingestion.models import EvidenceKind
from canonic.ingestion.source import evidence_from_docs

if TYPE_CHECKING:
    from pathlib import Path


class FixtureNotionPageSource:
    """In-process page source that returns pages loaded from a JSON fixture file.

    Implements :class:`NotionPageSource` without any network access so tests
    are fully offline and deterministic.
    """

    def __init__(self, path: Path) -> None:
        self._path = path

    async def list_pages(self) -> list[dict[str, Any]]:
        return json.loads(self._path.read_text())


@pytest.fixture
def page_source(notion_pages_path: Path) -> FixtureNotionPageSource:
    return FixtureNotionPageSource(notion_pages_path)


@pytest.fixture
def connector(page_source: FixtureNotionPageSource) -> GenericEvidenceConnector:
    return make_notion_connector(page_source=page_source)


class TestCapabilities:
    def test_declares_extract_evidence(self, connector: GenericEvidenceConnector) -> None:
        assert Capability.EXTRACT_EVIDENCE in connector.capabilities()

    def test_declares_test_connection(self, connector: GenericEvidenceConnector) -> None:
        assert Capability.TEST_CONNECTION in connector.capabilities()

    def test_does_not_declare_extract_definitions(
        self, connector: GenericEvidenceConnector
    ) -> None:
        assert Capability.EXTRACT_DEFINITIONS not in connector.capabilities()

    def test_does_not_declare_introspect_schema(self, connector: GenericEvidenceConnector) -> None:
        assert Capability.INTROSPECT_SCHEMA not in connector.capabilities()


class TestTestConnection:
    async def test_ok_on_supported_version(self, connector: GenericEvidenceConnector) -> None:
        health = await connector.test_connection()
        assert health.status == "ok"
        assert health.message is not None

    async def test_error_on_unsupported_version(self, page_source: FixtureNotionPageSource) -> None:
        bad = make_notion_connector(page_source=page_source, api_version="2020-01-01")
        health = await bad.test_connection()
        assert health.status == "error"
        assert "unsupported" in (health.message or "").lower()
        assert "2020-01-01" in (health.message or "")

    async def test_error_message_lists_supported_versions(
        self, page_source: FixtureNotionPageSource
    ) -> None:
        bad = make_notion_connector(page_source=page_source, api_version="2020-01-01")
        health = await bad.test_connection()
        for v in SUPPORTED_API_VERSIONS:
            assert v in (health.message or "")


class TestUsageHintMapping:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("policy", UsageHint.POLICY),
            ("caveat", UsageHint.CAVEAT),
            ("reference", UsageHint.REFERENCE),
            ("definition", UsageHint.DEFINITION),
            ("POLICY", UsageHint.POLICY),
            ("Policy", UsageHint.POLICY),
        ],
    )
    def test_known_values(self, raw: str, expected: UsageHint) -> None:
        assert _usage_hint_for(raw, "page-id") == expected

    def test_none_defaults_to_reference(self) -> None:
        assert _usage_hint_for(None, "page-id") == UsageHint.REFERENCE

    def test_empty_string_defaults_to_reference(self) -> None:
        assert _usage_hint_for("", "page-id") == UsageHint.REFERENCE

    def test_unknown_value_defaults_to_reference_with_warning(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.WARNING):
            result = _usage_hint_for("unknown_kind", "page-id")
        assert result == UsageHint.REFERENCE
        assert "unknown_kind" in caplog.text


class TestExtractEvidence:
    async def test_ac1_policy_page_produces_doc_evidence_with_usage_hint_policy(
        self, connector: GenericEvidenceConnector
    ) -> None:
        docs = await connector.extract_evidence()
        policy_docs = [d for d in docs if d.usage_hint == UsageHint.POLICY]
        assert len(policy_docs) == 1
        doc = policy_docs[0]
        assert doc.title == "Test account policy"
        assert doc.usage_hint == UsageHint.POLICY
        assert "customers" in doc.topic_refs
        assert "test_accounts" in doc.topic_refs
        assert doc.native_ref == "notion:page:abc123"
        assert doc.kind == "doc_evidence"
        assert doc.source == "notion_wiki"

    async def test_topic_refs_are_candidates_only(
        self, connector: GenericEvidenceConnector
    ) -> None:
        docs = await connector.extract_evidence()
        policy_doc = next(d for d in docs if d.usage_hint == UsageHint.POLICY)
        assert set(policy_doc.topic_refs) == {"customers", "test_accounts"}

    async def test_caveat_page_usage_hint(self, connector: GenericEvidenceConnector) -> None:
        docs = await connector.extract_evidence()
        caveat_docs = [d for d in docs if d.usage_hint == UsageHint.CAVEAT]
        assert len(caveat_docs) == 1
        assert caveat_docs[0].native_ref == "notion:page:def456"

    async def test_missing_canonic_type_defaults_to_reference(
        self, connector: GenericEvidenceConnector
    ) -> None:
        docs = await connector.extract_evidence()
        glossary = next(d for d in docs if "glossary" in d.title.lower())
        assert glossary.usage_hint == UsageHint.REFERENCE

    async def test_unrecognized_canonic_type_defaults_to_reference_with_warning(
        self, connector: GenericEvidenceConnector, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.WARNING):
            docs = await connector.extract_evidence()
        unknown_docs = [d for d in docs if "unknown" in d.title.lower()]
        assert len(unknown_docs) == 1
        assert unknown_docs[0].usage_hint == UsageHint.REFERENCE
        assert "unknown_kind" in caplog.text

    async def test_native_ref_format(self, connector: GenericEvidenceConnector) -> None:
        docs = await connector.extract_evidence()
        for doc in docs:
            assert doc.native_ref.startswith("notion:page:")

    async def test_source_fingerprint_present(self, connector: GenericEvidenceConnector) -> None:
        docs = await connector.extract_evidence()
        for doc in docs:
            assert doc.source_fingerprint is not None
            assert doc.source_fingerprint.startswith("sha256:")

    async def test_fingerprints_are_stable_across_calls(
        self, page_source: FixtureNotionPageSource
    ) -> None:
        c1 = make_notion_connector(page_source=page_source)
        c2 = make_notion_connector(page_source=page_source)
        docs1 = await c1.extract_evidence()
        docs2 = await c2.extract_evidence()
        fps1 = {d.native_ref: d.source_fingerprint for d in docs1}
        fps2 = {d.native_ref: d.source_fingerprint for d in docs2}
        assert fps1 == fps2

    async def test_unsupported_version_raises_connection_error(
        self, page_source: FixtureNotionPageSource
    ) -> None:
        from canonic.exc import ConnectionError as CanonicConnectionError
        from canonic.exc import UnsupportedSourceVersionError

        bad = make_notion_connector(page_source=page_source, api_version="2020-01-01")
        with pytest.raises(UnsupportedSourceVersionError, match="unsupported") as excinfo:
            await bad.extract_evidence()
        exc = excinfo.value
        assert exc.detected == "2020-01-01"
        assert exc.exit_code == 13
        assert isinstance(exc, CanonicConnectionError)

    async def test_doc_evidence_round_trips(self, connector: GenericEvidenceConnector) -> None:
        docs = await connector.extract_evidence()
        for doc in docs:
            round_tripped = DocEvidence.model_validate(doc.model_dump(mode="json"))
            assert round_tripped == doc

    async def test_no_notion_native_keys_in_dump(self, connector: GenericEvidenceConnector) -> None:
        docs = await connector.extract_evidence()
        notion_keys = {"object", "url", "properties", "_body"}
        for doc in docs:
            dumped = set(doc.model_dump(mode="json").keys())
            assert not dumped & notion_keys, f"Notion-native keys leaked: {dumped & notion_keys}"

    async def test_all_pages_extracted(self, connector: GenericEvidenceConnector) -> None:
        docs = await connector.extract_evidence()
        assert len(docs) == 4


class TestEvidenceFromDocsSeam:
    async def test_emits_doc_evidence_items(self, connector: GenericEvidenceConnector) -> None:
        items = await evidence_from_docs(connector, "notion_wiki")
        assert all(item.kind == EvidenceKind.DOC_EVIDENCE for item in items)

    async def test_source_stamped_correctly(self, connector: GenericEvidenceConnector) -> None:
        items = await evidence_from_docs(connector, "notion_wiki")
        assert all(item.source == "notion_wiki" for item in items)

    async def test_source_override(self, page_source: FixtureNotionPageSource) -> None:
        c = make_notion_connector(page_source=page_source, source="my_notion")
        items = await evidence_from_docs(c, "my_notion")
        assert all(item.source == "my_notion" for item in items)

    async def test_payload_contains_doc_evidence_fields(
        self, connector: GenericEvidenceConnector
    ) -> None:
        items = await evidence_from_docs(connector, "notion_wiki")
        for item in items:
            assert "title" in item.payload
            assert "body" in item.payload
            assert "usage_hint" in item.payload
            assert "topic_refs" in item.payload
            assert "native_ref" in item.payload
