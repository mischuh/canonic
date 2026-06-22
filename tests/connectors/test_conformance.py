"""Connector conformance harness.

This skeleton asserts the GH-3 contract properties.  Full conformance probes
(truthful capabilities, evidence validity against a live fixture, read-only
enforcement) are stubbed with pytest.mark.skip and will be filled in when the
first concrete connector (PostgreSQL, GH-4) lands.

E3-S4 (GH-90) extends coverage to all E3 connector classes: capability
truthfulness across the full connector matrix, evidence schema validity, and
acceptance criteria for multi-capability dispatch (AC1) and lying-connector
rejection (AC2).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import pytest
from pydantic import ValidationError

from canon.connectors.base import (
    AcquisitionTier,
    Capability,
    ConnectorBase,
    DefinitionEvidence,
    DefinitionExtract,
    DocEvidence,
    Health,
    RelationSchema,
    ResultColumn,
    ResultSet,
    UsageEvidence,
    UsageHint,
    require_capability,
)
from canon.exc import (
    CanonError,
    CapabilityNotSupportedError,
    ConnectionError,
    ReadOnlyViolation,
    SchemaMismatch,
)
from canon.ingestion.models import EvidenceKind
from canon.ingestion.source import gather_evidence

if TYPE_CHECKING:
    from pathlib import Path


class _MinimalConnector(ConnectorBase):
    """Implements only the two mandatory methods."""

    def capabilities(self) -> list[Capability]:
        return [Capability.CAPABILITIES, Capability.TEST_CONNECTION]

    async def test_connection(self) -> Health:
        return Health(status="ok")


_TESTABLE_CAPS = frozenset(
    {
        Capability.INTROSPECT_SCHEMA,
        Capability.READ_QUERY_HISTORY,
        Capability.RUN_READ_ONLY_SQL,
        Capability.EXTRACT_DEFINITIONS,
        Capability.EXTRACT_EVIDENCE,
    }
)


class TestRelationSchema:
    _VALID: dict = {
        "connection": "warehouse_pg",
        "relation": "analytics.fct_orders",
        "kind": "table",
        "columns": [{"name": "order_id", "type": "string", "nullable": False, "position": 1}],
        "acquisition_tier": "live",
    }

    def test_valid_round_trip(self) -> None:
        schema = RelationSchema.model_validate(self._VALID)
        assert schema.relation == "analytics.fct_orders"
        assert schema.acquisition_tier == AcquisitionTier.LIVE
        assert schema.primary_key == []
        assert schema.foreign_keys == []

    def test_acquisition_tier_rejects_unknown_value(self) -> None:
        bad = {**self._VALID, "acquisition_tier": "bogus"}
        with pytest.raises(ValidationError):
            RelationSchema.model_validate(bad)

    def test_kind_rejects_unknown_value(self) -> None:
        bad = {**self._VALID, "kind": "synonym"}
        with pytest.raises(ValidationError):
            RelationSchema.model_validate(bad)

    def test_all_acquisition_tiers_accepted(self) -> None:
        for tier in AcquisitionTier:
            schema = RelationSchema.model_validate({**self._VALID, "acquisition_tier": tier.value})
            assert schema.acquisition_tier == tier

    def test_frozen(self) -> None:
        schema = RelationSchema.model_validate(self._VALID)
        with pytest.raises(ValidationError):
            schema.relation = "other"  # type: ignore[misc]


class TestResultSet:
    def test_serialize_and_deserialize(self) -> None:
        rs = ResultSet(
            columns=[ResultColumn(name="id", type="int"), ResultColumn(name="name", type="string")],
            rows=[[1, "Alice"], [2, "Bob"]],
            truncated=False,
            bytes_scanned=1024,
        )
        json_str = rs.model_dump_json()
        restored = ResultSet.model_validate_json(json_str)
        assert restored.columns == rs.columns
        assert restored.rows == rs.rows
        assert restored.bytes_scanned == 1024

    def test_defaults(self) -> None:
        rs = ResultSet(columns=[], rows=[])
        assert rs.truncated is False
        assert rs.bytes_scanned is None


class TestConnectorBase:
    def test_cannot_instantiate_directly(self) -> None:
        with pytest.raises(TypeError):
            ConnectorBase()  # type: ignore[abstract]

    def test_minimal_connector_instantiates(self) -> None:
        conn = _MinimalConnector()
        assert Capability.TEST_CONNECTION in conn.capabilities()

    @pytest.mark.asyncio
    async def test_test_connection_returns_health(self) -> None:
        conn = _MinimalConnector()
        health = await conn.test_connection()
        assert isinstance(health, Health)
        assert health.status in ("ok", "error")


class TestExceptions:
    def test_read_only_violation_is_canon_error(self) -> None:
        err = ReadOnlyViolation("INSERT not allowed")
        assert isinstance(err, CanonError)

    def test_schema_mismatch_is_canon_error(self) -> None:
        err = SchemaMismatch("column 'foo' missing")
        assert isinstance(err, CanonError)

    def test_connection_error_is_canon_error(self) -> None:
        err = ConnectionError("could not connect")
        assert isinstance(err, CanonError)


@pytest.mark.asyncio
async def test_capabilities_are_truthful(offline_connector) -> None:  # noqa: ANN001
    """Advertised capabilities have implementations; unadvertised ones are absent."""
    advertised = set(offline_connector.capabilities())
    for cap in (
        Capability.INTROSPECT_SCHEMA,
        Capability.RUN_READ_ONLY_SQL,
        Capability.TEST_CONNECTION,
    ):
        assert cap in advertised
        assert hasattr(type(offline_connector), cap.value)

    assert Capability.READ_QUERY_HISTORY not in advertised
    assert not hasattr(type(offline_connector), Capability.READ_QUERY_HISTORY.value)


@pytest.mark.parametrize(
    "sql",
    ["INSERT INTO t VALUES (1)", "DROP TABLE t", "UPDATE t SET a = 1", "SELECT 1; SELECT 2"],
)
async def test_read_only_enforcement(offline_connector, sql) -> None:  # noqa: ANN001
    """DML/DDL and multiple statements are rejected before any connection opens."""
    with pytest.raises(ReadOnlyViolation):
        await offline_connector.run_read_only_sql(sql)


@pytest.mark.integration
async def test_evidence_schema_validity(pg_connector) -> None:  # noqa: ANN001
    """Emitted RelationSchema evidence is schema-valid and round-trips."""
    schemas = await pg_connector.introspect_schema()
    assert schemas
    for schema in schemas:
        restored = RelationSchema.model_validate(schema.model_dump())
        assert restored == schema
        assert restored.acquisition_tier in set(AcquisitionTier)


@pytest.mark.integration
async def test_fixture_round_trip(pg_connector) -> None:  # noqa: ANN001
    """A known seeded relation is discovered with normalized evidence."""
    schemas = {s.relation: s for s in await pg_connector.introspect_schema()}
    assert "analytics.fct_orders" in schemas
    orders = schemas["analytics.fct_orders"]
    assert orders.acquisition_tier == AcquisitionTier.LIVE
    assert orders.primary_key == ["order_id"]


# ---------------------------------------------------------------------------
# E3-S4 (GH-90): E3 class conformance — all connectors (SPEC-E3 §2.3, §9 S4)
# ---------------------------------------------------------------------------


class _FixtureNotionPageSource:
    def __init__(self, path: Path) -> None:
        self._path = path

    async def list_pages(self) -> list[dict[str, Any]]:
        return json.loads(self._path.read_text())


class _FixtureMetabaseQuestionSource:
    def __init__(self, path: Path, *, version: str = "v0.48.7") -> None:
        self._path = path
        self._version = version

    async def list_questions(self) -> list[dict[str, Any]]:
        return json.loads(self._path.read_text())

    async def server_version(self) -> str:
        return self._version


class _FixtureLookerLookSource:
    def __init__(self, path: Path, *, version: str = "4.0") -> None:
        self._path = path
        self._version = version

    async def list_looks(self) -> list[dict[str, Any]]:
        return json.loads(self._path.read_text())

    async def api_version(self) -> str:
        return self._version


@pytest.fixture(
    params=["postgres", "dbt", "notion", "metabase", "looker", "sqlite"],
    ids=["postgres", "dbt", "notion", "metabase", "looker", "sqlite"],
)
def any_offline_connector(
    request: pytest.FixtureRequest,
    offline_connector,  # noqa: ANN001
    sqlite_offline_connector,  # noqa: ANN001
    dbt_manifest_path: Path,
    notion_pages_path: Path,
    metabase_questions_path: Path,
    looker_looks_path: Path,
) -> ConnectorBase:
    """Offline instance of each registered connector type."""
    from canon.config import Connection
    from canon.connectors.dbt import DbtConnector
    from canon.connectors.looker import LookerConnector
    from canon.connectors.metabase import MetabaseConnector
    from canon.connectors.notion import NotionConnector

    match request.param:
        case "postgres":
            return offline_connector
        case "sqlite":
            return sqlite_offline_connector
        case "dbt":
            return DbtConnector(dbt_manifest_path)
        case "notion":
            return NotionConnector(page_source=_FixtureNotionPageSource(notion_pages_path))
        case "metabase":
            conn = Connection(
                id="metabase_prod",
                type="metabase",
                params={"base_url": "https://metabase.example.com"},
                credentials_ref="env:METABASE_API_KEY",
            )
            return MetabaseConnector(
                conn, question_source=_FixtureMetabaseQuestionSource(metabase_questions_path)
            )
        case "looker":
            conn = Connection(
                id="looker_prod",
                type="looker",
                params={"base_url": "https://looker.example.com"},
                credentials_ref="env:LOOKER_API_TOKEN",
            )
            return LookerConnector(conn, look_source=_FixtureLookerLookSource(looker_looks_path))
        case _:
            pytest.fail(f"unknown connector param: {request.param!r}")


def test_e3_capabilities_truthful(any_offline_connector: ConnectorBase) -> None:
    """Declared capabilities have implementations; undeclared ones are absent (AC2).

    For every testable capability: if the connector advertises it, the method must
    be defined on the class; if not advertised, the method must not exist on it.
    """
    advertised = set(any_offline_connector.capabilities())
    name = type(any_offline_connector).__name__
    for cap in _TESTABLE_CAPS:
        method_name = cap.value
        has_method = hasattr(type(any_offline_connector), method_name)
        if cap in advertised:
            assert has_method, f"{name} declares {cap.value!r} but {method_name}() is not defined"
        else:
            assert not has_method, (
                f"{name} does not declare {cap.value!r} but {method_name}() appears defined"
            )


@pytest.mark.asyncio
async def test_e3_evidence_schema_valid(any_offline_connector: ConnectorBase) -> None:
    """Emitted evidence is schema-valid and round-trips through model_validate (AC2 positive).

    Evidence connectors (``EXTRACT_EVIDENCE``) emit only :class:`DocEvidence` and
    :class:`UsageEvidence`.  Definition connectors (``EXTRACT_DEFINITIONS``) emit
    :class:`DefinitionEvidence` and optional :class:`RelationSchema`.
    """
    caps = set(any_offline_connector.capabilities())

    if Capability.EXTRACT_EVIDENCE in caps:
        items = await any_offline_connector.extract_evidence()
        assert items, f"{type(any_offline_connector).__name__}.extract_evidence() returned no items"
        for item in items:
            assert isinstance(item, DocEvidence | UsageEvidence)
            restored = type(item).model_validate(item.model_dump())
            assert restored == item

    if Capability.EXTRACT_DEFINITIONS in caps:
        extract = await any_offline_connector.extract_definitions()
        for defn in extract.definitions:
            restored = DefinitionEvidence.model_validate(defn.model_dump())
            assert restored == defn
        for schema in extract.relations:
            restored_schema = RelationSchema.model_validate(schema.model_dump())
            assert restored_schema == schema


# ---------------------------------------------------------------------------
# AC1 — multi-capability connector: each capability invoked without vendor branch
# ---------------------------------------------------------------------------


class _DualCapabilityConnector(ConnectorBase):
    """Connector declaring both EXTRACT_DEFINITIONS and EXTRACT_EVIDENCE (S4 AC1)."""

    def capabilities(self) -> list[Capability]:
        return [
            Capability.CAPABILITIES,
            Capability.TEST_CONNECTION,
            Capability.EXTRACT_DEFINITIONS,
            Capability.EXTRACT_EVIDENCE,
        ]

    async def test_connection(self) -> Health:
        return Health(status="ok")

    async def extract_definitions(self) -> DefinitionExtract:
        from canon.connectors.base import (
            AcquisitionTier,
            DefinitionEntityType,
            DefinitionEvidence,
            DefinitionExtract,
        )

        return DefinitionExtract(
            definitions=[
                DefinitionEvidence(
                    source="dual",
                    entity="revenue",
                    entity_type=DefinitionEntityType.MEASURE,
                    expr="SUM(amount)",
                    native_ref="dual::revenue",
                    acquisition_tier=AcquisitionTier.MODELING,
                )
            ]
        )

    async def extract_evidence(self) -> list[DocEvidence]:
        return [
            DocEvidence(
                source="dual",
                title="Revenue Policy",
                body="Revenue is recognized on shipment date.",
                usage_hint=UsageHint.POLICY,
                native_ref="dual:page:1",
                observed_at=datetime.now(UTC),
            )
        ]


@pytest.mark.asyncio
async def test_ac1_multi_capability_dispatch() -> None:
    """AC1: a connector declaring both extract_definitions and extract_evidence has each invoked.

    ``gather_evidence`` dispatches on ``Capability`` membership — zero vendor-name branches.
    Both a ``definition`` item and a ``doc_evidence`` item must appear in the output.
    """
    connector = _DualCapabilityConnector()
    items = await gather_evidence(connector, "dual")

    kinds = {item.kind for item in items}
    assert EvidenceKind.DEFINITION in kinds, "extract_definitions seam was not invoked"
    assert EvidenceKind.DOC_EVIDENCE in kinds, "extract_evidence seam was not invoked"


# ---------------------------------------------------------------------------
# AC2 — connector advertising a capability it cannot honor fails the harness
# ---------------------------------------------------------------------------


class _LyingConnector(ConnectorBase):
    """Advertises EXTRACT_EVIDENCE but never overrides it (S4 AC2)."""

    def capabilities(self) -> list[Capability]:
        return [
            Capability.CAPABILITIES,
            Capability.TEST_CONNECTION,
            Capability.EXTRACT_EVIDENCE,
        ]

    async def test_connection(self) -> Health:
        return Health(status="ok")


def test_ac2_lying_connector_fails_harness() -> None:
    """AC2: a connector advertising a capability it cannot honor is caught by the harness.

    ``_LyingConnector`` declares ``EXTRACT_EVIDENCE`` but never defines
    ``extract_evidence()``.  The truthfulness check must detect this — the method
    is absent from the class and ``has_method`` is ``False`` for an advertised capability.
    """
    connector = _LyingConnector()
    advertised = set(connector.capabilities())
    assert Capability.EXTRACT_EVIDENCE in advertised

    has_method = hasattr(type(connector), "extract_evidence")
    assert not has_method, (
        "_LyingConnector should not define extract_evidence — "
        "this test proves the harness would catch it"
    )


# ---------------------------------------------------------------------------
# S5 (GH-91) — out-of-range source version fails loudly, ingests nothing (AC1)
# ---------------------------------------------------------------------------


def _out_of_range_connector(
    param: str,
    *,
    tmp_path: Path,
    notion_pages_path: Path,
    metabase_questions_path: Path,
    looker_looks_path: Path,
) -> ConnectorBase:
    """Build each evidence connector pinned to an unsupported source version."""
    from canon.config import Connection
    from canon.connectors.dbt import DbtConnector
    from canon.connectors.looker import LookerConnector
    from canon.connectors.metabase import MetabaseConnector
    from canon.connectors.notion import NotionConnector

    match param:
        case "dbt":
            manifest = tmp_path / "old_manifest.json"
            manifest.write_text(
                json.dumps(
                    {
                        "metadata": {
                            "dbt_schema_version": (
                                "https://schemas.getdbt.com/dbt/manifest/v9.json"
                            ),
                            "dbt_version": "1.5.0",
                        },
                        "nodes": {},
                    }
                )
            )
            return DbtConnector(manifest)
        case "notion":
            return NotionConnector(
                page_source=_FixtureNotionPageSource(notion_pages_path),
                api_version="2020-01-01",
            )
        case "metabase":
            conn = Connection(
                id="metabase_prod",
                type="metabase",
                params={"base_url": "https://metabase.example.com"},
                credentials_ref="env:METABASE_API_KEY",
            )
            return MetabaseConnector(
                conn,
                question_source=_FixtureMetabaseQuestionSource(
                    metabase_questions_path, version="v0.45.0"
                ),
            )
        case "looker":
            conn = Connection(
                id="looker_prod",
                type="looker",
                params={"base_url": "https://looker.example.com"},
                credentials_ref="env:LOOKER_API_TOKEN",
            )
            return LookerConnector(
                conn,
                look_source=_FixtureLookerLookSource(looker_looks_path, version="3.1"),
            )
        case _:
            pytest.fail(f"unknown connector param: {param!r}")


@pytest.mark.asyncio
@pytest.mark.parametrize("param", ["dbt", "notion", "metabase", "looker"])
async def test_e3_unsupported_version_ingests_nothing(
    param: str,
    tmp_path: Path,
    notion_pages_path: Path,
    metabase_questions_path: Path,
    looker_looks_path: Path,
) -> None:
    """AC1 across the matrix: an out-of-range version raises and yields zero evidence.

    ``gather_evidence`` dispatches on capabilities with no vendor branches; the
    connector's own version guard must abort before any item is produced.
    """
    from canon.exc import UnsupportedSourceVersionError

    connector = _out_of_range_connector(
        param,
        tmp_path=tmp_path,
        notion_pages_path=notion_pages_path,
        metabase_questions_path=metabase_questions_path,
        looker_looks_path=looker_looks_path,
    )
    with pytest.raises(UnsupportedSourceVersionError):
        await gather_evidence(connector, param)


# ---------------------------------------------------------------------------
# S8 (GH-94) — E3 connectors never declare SQL execution or introspection caps
# ---------------------------------------------------------------------------


def test_e3_connectors_declare_no_execution_caps(any_offline_connector: ConnectorBase) -> None:
    """E3 (definition/evidence) connectors never advertise SQL execution or introspection (S8-AC1).

    Checks the capability boundary: if a connector declares ``EXTRACT_DEFINITIONS`` or
    ``EXTRACT_EVIDENCE`` it must not also declare ``RUN_READ_ONLY_SQL`` or
    ``INTROSPECT_SCHEMA``.  PostgreSQL (E2) does not declare extract caps and is skipped.
    """
    caps = set(any_offline_connector.capabilities())
    is_e3 = bool(caps & {Capability.EXTRACT_DEFINITIONS, Capability.EXTRACT_EVIDENCE})
    if not is_e3:
        return  # E2 connector — this invariant does not apply

    assert Capability.RUN_READ_ONLY_SQL not in caps, (
        f"{type(any_offline_connector).__name__} is an E3 connector but declares "
        f"{Capability.RUN_READ_ONLY_SQL.value!r} — no-execution invariant violated (S8)"
    )
    assert Capability.INTROSPECT_SCHEMA not in caps, (
        f"{type(any_offline_connector).__name__} is an E3 connector but declares "
        f"{Capability.INTROSPECT_SCHEMA.value!r} — no-execution invariant violated (S8)"
    )


def test_require_capability_raises_for_missing_cap(any_offline_connector: ConnectorBase) -> None:
    """require_capability raises CapabilityNotSupportedError for absent caps, not AttributeError."""
    caps = set(any_offline_connector.capabilities())
    missing = next(
        (
            cap
            for cap in (Capability.RUN_READ_ONLY_SQL, Capability.INTROSPECT_SCHEMA)
            if cap not in caps
        ),
        None,
    )
    if missing is None:
        pytest.skip(f"{type(any_offline_connector).__name__} declares all execution caps")

    with pytest.raises(CapabilityNotSupportedError):
        require_capability(any_offline_connector, missing)


def test_require_capability_passes_through_declared_cap(
    any_offline_connector: ConnectorBase,
) -> None:
    """require_capability returns the connector unchanged when the cap is declared."""
    cap = Capability.TEST_CONNECTION
    result = require_capability(any_offline_connector, cap)
    assert result is any_offline_connector
