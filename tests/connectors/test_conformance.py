"""Connector conformance harness.

This skeleton asserts the GH-3 contract properties.  Full conformance probes
(truthful capabilities, evidence validity against a live fixture, read-only
enforcement) are stubbed with pytest.mark.skip and will be filled in when the
first concrete connector (PostgreSQL, GH-4) lands.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from canon.connectors.base import (
    AcquisitionTier,
    Capability,
    ConnectorBase,
    Health,
    RelationSchema,
    ResultColumn,
    ResultSet,
)
from canon.exc import CanonError, ConnectionError, ReadOnlyViolation, SchemaMismatch

class _MinimalConnector(ConnectorBase):
    """Implements only the two mandatory methods."""

    def capabilities(self) -> list[Capability]:
        return [Capability.capabilities, Capability.test_connection]

    async def test_connection(self) -> Health:
        return Health(status="ok")


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
        assert schema.acquisition_tier == AcquisitionTier.live
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
        assert Capability.test_connection in conn.capabilities()

    @pytest.mark.asyncio
    async def test_test_connection_returns_health(self) -> None:
        conn = _MinimalConnector()
        health = await conn.test_connection()
        assert isinstance(health, Health)
        assert health.status in ("ok", "error")

    @pytest.mark.asyncio
    async def test_unimplemented_introspect_schema_raises(self) -> None:
        conn = _MinimalConnector()
        with pytest.raises(NotImplementedError):
            await conn.introspect_schema()

    @pytest.mark.asyncio
    async def test_unimplemented_run_read_only_sql_raises(self) -> None:
        conn = _MinimalConnector()
        with pytest.raises(NotImplementedError):
            await conn.run_read_only_sql("SELECT 1")

    @pytest.mark.asyncio
    async def test_unimplemented_read_query_history_raises(self) -> None:
        from datetime import UTC, datetime

        conn = _MinimalConnector()
        with pytest.raises(NotImplementedError):
            await conn.read_query_history(datetime.now(tz=UTC))


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
    """Advertised capabilities are overridden; unadvertised ones raise."""
    from datetime import UTC, datetime

    advertised = set(offline_connector.capabilities())
    for cap in (
        Capability.introspect_schema,
        Capability.run_read_only_sql,
        Capability.test_connection,
    ):
        assert cap in advertised
        method = getattr(type(offline_connector), cap.value)
        assert method is not getattr(ConnectorBase, cap.value)

    assert Capability.read_query_history not in advertised
    with pytest.raises(NotImplementedError):
        await offline_connector.read_query_history(datetime.now(tz=UTC))


@pytest.mark.parametrize(
    "sql",
    ["INSERT INTO t VALUES (1)", "DROP TABLE t", "UPDATE t SET a = 1", "SELECT 1; SELECT 2"],
)
def test_read_only_enforcement(offline_connector, sql) -> None:  # noqa: ANN001
    """DML/DDL and multiple statements are rejected before execution."""
    with pytest.raises(ReadOnlyViolation):
        offline_connector._assert_read_only(sql)


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
    assert orders.acquisition_tier == AcquisitionTier.live
    assert orders.primary_key == ["order_id"]
