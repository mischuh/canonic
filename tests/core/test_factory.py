"""Tests for ConnectorFactory and the builtin registry (canonic/connectors/factory.py, E2 S9)."""

from __future__ import annotations

import pytest

from canonic.config import CanonicConfig, Connection
from canonic.connectors.base import ConnectorBase, Health
from canonic.connectors.factory import ConnectorFactory, default_factory
from canonic.connectors.postgres import PostgresConnector
from canonic.exc import ConnectionError, UnknownConnectorType


@pytest.fixture
def config_with_pg(monkeypatch: pytest.MonkeyPatch) -> CanonicConfig:
    monkeypatch.setenv("PG_PASSWORD", "testpw")
    return CanonicConfig.model_validate(
        {
            "version": 1,
            "project": {"name": "test", "default_connection": "warehouse_pg"},
            "connections": [
                {
                    "id": "warehouse_pg",
                    "type": "postgres",
                    "params": {
                        "host": "localhost",
                        "port": 5432,
                        "dbname": "testdb",
                        "user": "test",
                    },
                    "credentials_ref": "env:PG_PASSWORD",
                }
            ],
            "llm": {
                "provider": "openai_compatible",
                "base_url": "http://localhost/v1",
                "model": "llama3",
            },
        }
    )


# --- AC1: factory.create dispatches to the right class ---------------------------


def test_create_postgres(monkeypatch: pytest.MonkeyPatch, config_with_pg: CanonicConfig) -> None:
    conn = config_with_pg.connections[0]
    connector = default_factory.create(conn)
    assert isinstance(connector, PostgresConnector)


def test_instantiate_postgres(
    monkeypatch: pytest.MonkeyPatch, config_with_pg: CanonicConfig
) -> None:
    conn = config_with_pg.connections[0]
    connector = default_factory.instantiate("postgres", conn)
    assert isinstance(connector, PostgresConnector)


def test_create_url_wraps_url_fetch_adapter() -> None:
    """The recurring-ingest path for URLs shares UrlFetchAdapter with `knowledge add`."""
    from canonic.connectors.evidence import GenericEvidenceConnector, NullExtractionSkill
    from canonic.connectors.web import UrlFetchAdapter

    conn = Connection(
        id="saas_kpi_glossary", type="url", params={"urls": ["https://example.com/kpis"]}
    )
    connector = default_factory.create(conn)

    assert isinstance(connector, GenericEvidenceConnector)
    assert isinstance(connector.extraction_skill, NullExtractionSkill)  # backfilled by ingest.py
    assert isinstance(connector._fetch_adapter, UrlFetchAdapter)  # noqa: SLF001 - white-box wiring check


def test_create_url_missing_urls_param_raises() -> None:
    conn = Connection(id="bad_url_conn", type="url", params={})
    with pytest.raises(ConnectionError, match="requires params.urls"):
        default_factory.create(conn)


# --- AC2: unknown type raises UnknownConnectorType with list of registered types --


def test_instantiate_unknown_type_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PG_PASSWORD", "pw")
    conn = Connection(id="x", type="snowflake", params={}, credentials_ref="env:PG_PASSWORD")
    with pytest.raises(UnknownConnectorType) as exc_info:
        default_factory.create(conn)
    err = exc_info.value
    assert err.type_name == "snowflake"
    assert "snowflake" in str(err)
    assert "postgres" in str(err)
    assert isinstance(err, ConnectionError)
    assert err.exit_code == 13


def test_instantiate_unknown_type_lists_registered(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PG_PASSWORD", "pw")
    conn = Connection(id="x", type="mystery", params={}, credentials_ref="env:PG_PASSWORD")
    with pytest.raises(UnknownConnectorType) as exc_info:
        default_factory.instantiate("mystery", conn)
    assert set(exc_info.value.known) == set(default_factory.registered_types())


# --- AC4: new connector registered without touching core logic --------------------


class _FakeConnector(ConnectorBase):
    def capabilities(self) -> list:
        return []

    async def test_connection(self) -> Health:
        return Health(status="ok")


def test_register_and_instantiate_new_type(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PG_PASSWORD", "pw")
    factory = ConnectorFactory()
    factory.register("fake", lambda _conn: _FakeConnector())
    conn = Connection(id="f", type="fake", params={}, credentials_ref="env:PG_PASSWORD")
    result = factory.create(conn)
    assert isinstance(result, _FakeConnector)


def test_register_unknown_in_isolated_factory(monkeypatch: pytest.MonkeyPatch) -> None:
    factory = ConnectorFactory()
    conn = Connection(id="x", type="postgres", params={})
    with pytest.raises(UnknownConnectorType):
        factory.create(conn)


# --- for_id: resolve by connection id or project default -------------------------


def test_for_id_default(config_with_pg: CanonicConfig) -> None:
    connector = default_factory.for_id(config_with_pg, connection_id=None)
    assert isinstance(connector, PostgresConnector)


def test_for_id_explicit(config_with_pg: CanonicConfig) -> None:
    connector = default_factory.for_id(config_with_pg, connection_id="warehouse_pg")
    assert isinstance(connector, PostgresConnector)


def test_for_id_unknown(config_with_pg: CanonicConfig) -> None:
    with pytest.raises(ConnectionError, match="unknown connection 'nonexistent'"):
        default_factory.for_id(config_with_pg, connection_id="nonexistent")


def test_for_id_no_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PG_PASSWORD", "pw")
    config = CanonicConfig.model_validate(
        {
            "version": 1,
            "project": {"name": "test"},
            "connections": [
                {
                    "id": "warehouse_pg",
                    "type": "postgres",
                    "params": {},
                    "credentials_ref": "env:PG_PASSWORD",
                }
            ],
            "llm": {
                "provider": "openai_compatible",
                "base_url": "http://localhost/v1",
                "model": "llama3",
            },
        }
    )
    with pytest.raises(ConnectionError, match="no connection specified"):
        default_factory.for_id(config, connection_id=None)


# --- registered_types helper -----------------------------------------------------


def test_registered_types_includes_builtins() -> None:
    types = default_factory.registered_types()
    assert "postgres" in types
    assert "dbt" in types
    assert "notion" in types
    assert "url" in types
    assert types == sorted(types)
