"""Tests for the Metabase evidence connector (SPEC-E3 §3.3, §5, §9 S3 AC1/AC2)."""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any

import pytest

from canon.connectors.base import (
    Capability,
    UsageEvidence,
    UsageRole,
)
from canon.connectors.metabase import (
    MetabaseConnector,
    _assign_role,
    _extract_expr,
    _extract_references,
)
from canon.ingestion.models import EvidenceKind
from canon.ingestion.source import evidence_from_docs

if TYPE_CHECKING:
    from pathlib import Path


class FixtureMetabaseQuestionSource:
    """In-process question source that returns questions from a JSON fixture.

    Implements :class:`MetabaseQuestionSource` without any network access so tests
    are fully offline and deterministic.  ``version`` can be overridden to test
    version-pinning behaviour.
    """

    def __init__(self, path: Path, *, version: str = "v0.48.7") -> None:
        self._path = path
        self._version = version

    async def list_questions(self) -> list[dict[str, Any]]:
        return json.loads(self._path.read_text())

    async def server_version(self) -> str:
        return self._version


@pytest.fixture
def question_source(metabase_questions_path: Path) -> FixtureMetabaseQuestionSource:
    return FixtureMetabaseQuestionSource(metabase_questions_path)


@pytest.fixture
def connector(question_source: FixtureMetabaseQuestionSource) -> MetabaseConnector:
    from canon.config import Connection

    conn = Connection(
        id="metabase_prod",
        type="metabase",
        params={"base_url": "https://metabase.example.com"},
        credentials_ref="env:METABASE_API_KEY",
    )
    return MetabaseConnector(conn, question_source=question_source)


class TestCapabilities:
    def test_declares_extract_evidence(self, connector: MetabaseConnector) -> None:
        assert Capability.EXTRACT_EVIDENCE in connector.capabilities()

    def test_declares_test_connection(self, connector: MetabaseConnector) -> None:
        assert Capability.TEST_CONNECTION in connector.capabilities()

    def test_does_not_declare_extract_definitions(self, connector: MetabaseConnector) -> None:
        assert Capability.EXTRACT_DEFINITIONS not in connector.capabilities()

    def test_does_not_declare_introspect_schema(self, connector: MetabaseConnector) -> None:
        assert Capability.INTROSPECT_SCHEMA not in connector.capabilities()


class TestTestConnection:
    async def test_ok_on_supported_version(self, connector: MetabaseConnector) -> None:
        health = await connector.test_connection()
        assert health.status == "ok"
        assert "0.48" in (health.message or "")

    async def test_error_on_old_version(self, metabase_questions_path: Path) -> None:
        from canon.config import Connection

        conn = Connection(
            id="metabase_prod",
            type="metabase",
            params={"base_url": "https://metabase.example.com"},
            credentials_ref="env:METABASE_API_KEY",
        )
        old_source = FixtureMetabaseQuestionSource(metabase_questions_path, version="v0.45.0")
        bad = MetabaseConnector(conn, question_source=old_source)
        health = await bad.test_connection()
        assert health.status == "error"
        assert "0.45" in (health.message or "")

    async def test_error_on_unparseable_version(self, metabase_questions_path: Path) -> None:
        from canon.config import Connection

        conn = Connection(
            id="metabase_prod",
            type="metabase",
            params={"base_url": "https://metabase.example.com"},
            credentials_ref="env:METABASE_API_KEY",
        )
        bad_source = FixtureMetabaseQuestionSource(metabase_questions_path, version="")
        bad = MetabaseConnector(conn, question_source=bad_source)
        health = await bad.test_connection()
        assert health.status == "error"


class TestExprExtraction:
    def test_native_sql_uses_query(self) -> None:
        card = {"id": 1, "dataset_query": {"type": "native", "native": {"query": "SELECT 1"}}}
        assert _extract_expr(card) == "SELECT 1"

    def test_native_sql_empty_query_returns_unknown(self) -> None:
        card = {"id": 1, "dataset_query": {"type": "native", "native": {"query": ""}}}
        assert _extract_expr(card) == "unknown"

    def test_mbql_sum_field_reconstructed(self) -> None:
        card = {
            "id": 1,
            "dataset_query": {
                "type": "query",
                "query": {
                    "source-table": 5,
                    "aggregation": [["sum", ["field", 42, {"base-type": "type/Float"}]]],
                },
            },
        }
        assert _extract_expr(card) == "sum(field:42)"

    def test_mbql_no_aggregation_returns_unknown(self) -> None:
        card = {
            "id": 1,
            "dataset_query": {"type": "query", "query": {"source-table": 5, "aggregation": []}},
        }
        assert _extract_expr(card) == "unknown"

    def test_unrecognized_type_returns_unknown_with_warning(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        card = {"id": 99, "dataset_query": {"type": "unknown_future_type"}}
        with caplog.at_level(logging.WARNING):
            result = _extract_expr(card)
        assert result == "unknown"
        assert "unrecognized" in caplog.text


class TestReferenceExtraction:
    def test_mbql_source_table_extracted(self) -> None:
        card = {
            "dataset_query": {
                "type": "query",
                "query": {"source-table": 5},
            }
        }
        assert _extract_references(card) == ["metabase:table:5"]

    def test_native_sql_returns_empty(self) -> None:
        card = {"dataset_query": {"type": "native", "native": {"query": "SELECT 1"}}}
        assert _extract_references(card) == []


class TestRoleAssignment:
    def test_unofficial_is_alternative(self) -> None:
        card = {"collection": {"authority_level": None}, "view_count": 1000}
        assert _assign_role(card) is UsageRole.ALTERNATIVE

    def test_official_but_low_views_is_alternative(self) -> None:
        card = {"collection": {"authority_level": "official"}, "view_count": 2}
        assert _assign_role(card) is UsageRole.ALTERNATIVE

    def test_official_high_views_is_trusted_example(self) -> None:
        card = {"collection": {"authority_level": "official"}, "view_count": 250}
        assert _assign_role(card) is UsageRole.TRUSTED_EXAMPLE

    def test_no_collection_is_alternative(self) -> None:
        card = {"collection": None, "view_count": 9999}
        assert _assign_role(card) is UsageRole.ALTERNATIVE

    def test_role_is_never_canonical(self) -> None:
        for card in [
            {"collection": {"authority_level": "official"}, "view_count": 9999},
            {"collection": None, "view_count": 0},
        ]:
            role = _assign_role(card)
            assert role in (UsageRole.ALTERNATIVE, UsageRole.TRUSTED_EXAMPLE)
            assert role.value != "canonical"


class TestExtractEvidence:
    async def test_all_questions_extracted(self, connector: MetabaseConnector) -> None:
        items = await connector.extract_evidence()
        assert len(items) == 3

    async def test_alternative_role_for_unofficial_question(
        self, connector: MetabaseConnector
    ) -> None:
        items = await connector.extract_evidence()
        q412 = next(e for e in items if e.artifact == "question:412")
        assert q412.role is UsageRole.ALTERNATIVE

    async def test_trusted_example_role_for_official_question(
        self, connector: MetabaseConnector
    ) -> None:
        items = await connector.extract_evidence()
        q413 = next(e for e in items if e.artifact == "question:413")
        assert q413.role is UsageRole.TRUSTED_EXAMPLE

    async def test_role_is_never_canonical(self, connector: MetabaseConnector) -> None:
        items = await connector.extract_evidence()
        for item in items:
            assert item.role.value != "canonical"

    async def test_native_ref_format(self, connector: MetabaseConnector) -> None:
        items = await connector.extract_evidence()
        for item in items:
            assert item.native_ref.startswith("metabase:question:")

    async def test_kind_is_usage_evidence(self, connector: MetabaseConnector) -> None:
        items = await connector.extract_evidence()
        for item in items:
            assert item.kind == "usage_evidence"

    async def test_source_fingerprint_present(self, connector: MetabaseConnector) -> None:
        items = await connector.extract_evidence()
        for item in items:
            assert item.source_fingerprint is not None
            assert item.source_fingerprint.startswith("sha256:")

    async def test_fingerprints_stable_across_calls(
        self, question_source: FixtureMetabaseQuestionSource
    ) -> None:
        from canon.config import Connection

        conn = Connection(
            id="metabase_prod",
            type="metabase",
            params={"base_url": "https://metabase.example.com"},
            credentials_ref="env:METABASE_API_KEY",
        )
        c1 = MetabaseConnector(conn, question_source=question_source)
        c2 = MetabaseConnector(conn, question_source=question_source)
        fps1 = {e.artifact: e.source_fingerprint for e in await c1.extract_evidence()}
        fps2 = {e.artifact: e.source_fingerprint for e in await c2.extract_evidence()}
        assert fps1 == fps2

    async def test_usage_evidence_round_trips(self, connector: MetabaseConnector) -> None:
        items = await connector.extract_evidence()
        for item in items:
            rt = UsageEvidence.model_validate(item.model_dump(mode="json"))
            assert rt == item

    async def test_no_vendor_native_keys_in_dump(self, connector: MetabaseConnector) -> None:
        items = await connector.extract_evidence()
        vendor_keys = {"dataset_query", "collection", "creator", "collection_id"}
        for item in items:
            dumped = set(item.model_dump(mode="json").keys())
            assert not dumped & vendor_keys, f"Vendor keys leaked: {dumped & vendor_keys}"

    async def test_frequency_from_view_count(self, connector: MetabaseConnector) -> None:
        items = await connector.extract_evidence()
        q412 = next(e for e in items if e.artifact == "question:412")
        assert q412.frequency == 87

    async def test_defines_has_expr(self, connector: MetabaseConnector) -> None:
        items = await connector.extract_evidence()
        for item in items:
            assert item.defines.expr  # never empty (may be "unknown")


class TestEvidenceFromDocsSeam:
    async def test_emits_usage_evidence_items(self, connector: MetabaseConnector) -> None:
        items = await evidence_from_docs(connector, "metabase_prod")
        assert all(item.kind == EvidenceKind.USAGE_EVIDENCE for item in items)

    async def test_source_stamped_correctly(self, connector: MetabaseConnector) -> None:
        items = await evidence_from_docs(connector, "metabase_prod")
        assert all(item.source == "metabase_prod" for item in items)

    async def test_payload_contains_usage_evidence_fields(
        self, connector: MetabaseConnector
    ) -> None:
        items = await evidence_from_docs(connector, "metabase_prod")
        for item in items:
            assert "artifact" in item.payload
            assert "defines" in item.payload
            assert "role" in item.payload
            assert "native_ref" in item.payload
