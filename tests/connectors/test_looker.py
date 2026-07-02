"""Tests for the Looker evidence connector (SPEC-E3 §3.3, §5, §9 S3)."""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any

import pytest

from canonic.connectors.base import (
    Capability,
    UsageEvidence,
    UsageRole,
)
from canonic.connectors.looker import (
    SUPPORTED_API_VERSION,
    LookerConnector,
    _assign_role,
    _extract_expr,
    _extract_references,
)
from canonic.ingestion.models import EvidenceKind
from canonic.ingestion.source import evidence_from_docs

if TYPE_CHECKING:
    from pathlib import Path


class FixtureLookerLookSource:
    """In-process look source that returns looks from a JSON fixture.

    Implements :class:`LookerLookSource` without any network access so tests
    are fully offline and deterministic.  ``version`` can be overridden to test
    version-pinning behaviour.
    """

    def __init__(self, path: Path, *, version: str = "4.0") -> None:
        self._path = path
        self._version = version

    async def list_looks(self) -> list[dict[str, Any]]:
        return json.loads(self._path.read_text())

    async def api_version(self) -> str:
        return self._version


@pytest.fixture
def look_source(looker_looks_path: Path) -> FixtureLookerLookSource:
    return FixtureLookerLookSource(looker_looks_path)


@pytest.fixture
def connector(look_source: FixtureLookerLookSource) -> LookerConnector:
    from canonic.config import Connection

    conn = Connection(
        id="looker_prod",
        type="looker",
        params={"base_url": "https://looker.example.com"},
        credentials_ref="env:LOOKER_API_TOKEN",
    )
    return LookerConnector(conn, look_source=look_source)


class TestCapabilities:
    def test_declares_extract_evidence(self, connector: LookerConnector) -> None:
        assert Capability.EXTRACT_EVIDENCE in connector.capabilities()

    def test_declares_test_connection(self, connector: LookerConnector) -> None:
        assert Capability.TEST_CONNECTION in connector.capabilities()

    def test_does_not_declare_extract_definitions(self, connector: LookerConnector) -> None:
        assert Capability.EXTRACT_DEFINITIONS not in connector.capabilities()

    def test_does_not_declare_introspect_schema(self, connector: LookerConnector) -> None:
        assert Capability.INTROSPECT_SCHEMA not in connector.capabilities()


class TestTestConnection:
    async def test_ok_on_supported_version(self, connector: LookerConnector) -> None:
        health = await connector.test_connection()
        assert health.status == "ok"
        assert SUPPORTED_API_VERSION in (health.message or "")

    async def test_error_on_unsupported_version(self, looker_looks_path: Path) -> None:
        from canonic.config import Connection

        conn = Connection(
            id="looker_prod",
            type="looker",
            params={"base_url": "https://looker.example.com"},
            credentials_ref="env:LOOKER_API_TOKEN",
        )
        old_source = FixtureLookerLookSource(looker_looks_path, version="3.1")
        bad = LookerConnector(conn, look_source=old_source)
        health = await bad.test_connection()
        assert health.status == "error"
        assert "3.1" in (health.message or "")

    async def test_extract_raises_on_unsupported_version(self, looker_looks_path: Path) -> None:
        """SPEC-E3 §6, S5 — out-of-range server ingests nothing (AC1)."""
        from canonic.config import Connection
        from canonic.exc import ConnectionError, UnsupportedSourceVersionError

        conn = Connection(
            id="looker_prod",
            type="looker",
            params={"base_url": "https://looker.example.com"},
            credentials_ref="env:LOOKER_API_TOKEN",
        )
        old_source = FixtureLookerLookSource(looker_looks_path, version="3.1")
        bad = LookerConnector(conn, look_source=old_source)
        with pytest.raises(UnsupportedSourceVersionError) as excinfo:
            await bad.extract_evidence()
        exc = excinfo.value
        assert exc.detected == "3.1"
        assert exc.exit_code == 13
        assert isinstance(exc, ConnectionError)


class TestExprExtraction:
    def test_measures_used_as_expr(self) -> None:
        look = {"id": 1, "query": {"measures": ["orders.revenue"], "fields": []}}
        assert _extract_expr(look) == "orders.revenue"

    def test_multiple_measures_joined(self) -> None:
        look = {"id": 1, "query": {"measures": ["a.x", "a.y"], "fields": []}}
        assert _extract_expr(look) == "a.x, a.y"

    def test_falls_back_to_fields_when_no_measures(self) -> None:
        look = {"id": 1, "query": {"measures": [], "fields": ["a.date"]}}
        assert _extract_expr(look) == "a.date"

    def test_no_measures_or_fields_returns_unknown(self, caplog: pytest.LogCaptureFixture) -> None:
        look = {"id": 99, "query": {"measures": [], "fields": []}}
        with caplog.at_level(logging.WARNING):
            result = _extract_expr(look)
        assert result == "unknown"
        assert "99" in caplog.text


class TestReferenceExtraction:
    def test_model_and_view_combined(self) -> None:
        look = {"query": {"model": "analytics", "view": "fct_orders"}}
        assert _extract_references(look) == ["analytics.fct_orders"]

    def test_model_only(self) -> None:
        look = {"query": {"model": "analytics", "view": ""}}
        assert _extract_references(look) == ["analytics"]

    def test_no_model_returns_empty(self) -> None:
        look = {"query": {"model": "", "view": ""}}
        assert _extract_references(look) == []


class TestRoleAssignment:
    def test_non_public_is_alternative(self) -> None:
        look = {"public": False, "view_count": 9999}
        assert _assign_role(look) is UsageRole.ALTERNATIVE

    def test_public_low_views_is_alternative(self) -> None:
        look = {"public": True, "view_count": 2}
        assert _assign_role(look) is UsageRole.ALTERNATIVE

    def test_public_high_views_is_trusted_example(self) -> None:
        look = {"public": True, "view_count": 150}
        assert _assign_role(look) is UsageRole.TRUSTED_EXAMPLE

    def test_role_is_never_canonical(self) -> None:
        for look in [
            {"public": True, "view_count": 9999},
            {"public": False, "view_count": 0},
        ]:
            role = _assign_role(look)
            assert role in (UsageRole.ALTERNATIVE, UsageRole.TRUSTED_EXAMPLE)
            assert role.value != "canonical"


class TestExtractEvidence:
    async def test_all_looks_extracted(self, connector: LookerConnector) -> None:
        items = await connector.extract_evidence()
        assert len(items) == 3

    async def test_non_public_look_is_alternative(self, connector: LookerConnector) -> None:
        items = await connector.extract_evidence()
        look1 = next(e for e in items if e.artifact == "look:1")
        assert look1.role is UsageRole.ALTERNATIVE

    async def test_public_popular_look_is_trusted_example(self, connector: LookerConnector) -> None:
        items = await connector.extract_evidence()
        look2 = next(e for e in items if e.artifact == "look:2")
        assert look2.role is UsageRole.TRUSTED_EXAMPLE

    async def test_role_is_never_canonical(self, connector: LookerConnector) -> None:
        items = await connector.extract_evidence()
        for item in items:
            assert item.role.value != "canonical"

    async def test_native_ref_format(self, connector: LookerConnector) -> None:
        items = await connector.extract_evidence()
        for item in items:
            assert item.native_ref.startswith("looker:look:")

    async def test_kind_is_usage_evidence(self, connector: LookerConnector) -> None:
        items = await connector.extract_evidence()
        for item in items:
            assert item.kind == "usage_evidence"

    async def test_source_fingerprint_present(self, connector: LookerConnector) -> None:
        items = await connector.extract_evidence()
        for item in items:
            assert item.source_fingerprint is not None
            assert item.source_fingerprint.startswith("sha256:")

    async def test_fingerprints_stable_across_calls(
        self, look_source: FixtureLookerLookSource
    ) -> None:
        from canonic.config import Connection

        conn = Connection(
            id="looker_prod",
            type="looker",
            params={"base_url": "https://looker.example.com"},
            credentials_ref="env:LOOKER_API_TOKEN",
        )
        c1 = LookerConnector(conn, look_source=look_source)
        c2 = LookerConnector(conn, look_source=look_source)
        fps1 = {e.artifact: e.source_fingerprint for e in await c1.extract_evidence()}
        fps2 = {e.artifact: e.source_fingerprint for e in await c2.extract_evidence()}
        assert fps1 == fps2

    async def test_usage_evidence_round_trips(self, connector: LookerConnector) -> None:
        items = await connector.extract_evidence()
        for item in items:
            rt = UsageEvidence.model_validate(item.model_dump(mode="json"))
            assert rt == item

    async def test_no_vendor_native_keys_in_dump(self, connector: LookerConnector) -> None:
        items = await connector.extract_evidence()
        vendor_keys = {"query", "public", "updated_at", "created_at"}
        for item in items:
            dumped = set(item.model_dump(mode="json").keys())
            assert not dumped & vendor_keys, f"Vendor keys leaked: {dumped & vendor_keys}"

    async def test_references_contain_model_view(self, connector: LookerConnector) -> None:
        items = await connector.extract_evidence()
        look1 = next(e for e in items if e.artifact == "look:1")
        assert "analytics.fct_orders" in look1.defines.references


class TestEvidenceFromDocsSeam:
    async def test_emits_usage_evidence_items(self, connector: LookerConnector) -> None:
        items = await evidence_from_docs(connector, "looker_prod")
        assert all(item.kind == EvidenceKind.USAGE_EVIDENCE for item in items)

    async def test_source_stamped_correctly(self, connector: LookerConnector) -> None:
        items = await evidence_from_docs(connector, "looker_prod")
        assert all(item.source == "looker_prod" for item in items)

    async def test_payload_contains_usage_evidence_fields(self, connector: LookerConnector) -> None:
        items = await evidence_from_docs(connector, "looker_prod")
        for item in items:
            assert "artifact" in item.payload
            assert "defines" in item.payload
            assert "role" in item.payload
            assert "native_ref" in item.payload
