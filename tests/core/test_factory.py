"""Tests for the connector factory (canon/connectors/factory.py)."""

from __future__ import annotations

import pytest

from canon.config import CanonConfig
from canon.connectors.factory import connector_by_id, connector_for
from canon.connectors.postgres import PostgresConnector
from canon.exc import ConnectionError


@pytest.fixture
def config_with_pg(monkeypatch: pytest.MonkeyPatch) -> CanonConfig:
    monkeypatch.setenv("PG_PASSWORD", "testpw")
    return CanonConfig.model_validate(
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


def test_connector_for_postgres(
    monkeypatch: pytest.MonkeyPatch, config_with_pg: CanonConfig
) -> None:
    conn = config_with_pg.connections[0]
    connector = connector_for(conn)
    assert isinstance(connector, PostgresConnector)


def test_connector_for_unknown_type(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PG_PASSWORD", "pw")
    from canon.config import Connection

    conn = Connection(id="x", type="snowflake", params={}, credentials_ref="env:PG_PASSWORD")
    with pytest.raises(ConnectionError, match="unsupported type 'snowflake'"):
        connector_for(conn)


def test_connector_by_id_default(config_with_pg: CanonConfig) -> None:
    connector = connector_by_id(config_with_pg, connection_id=None)
    assert isinstance(connector, PostgresConnector)


def test_connector_by_id_explicit(config_with_pg: CanonConfig) -> None:
    connector = connector_by_id(config_with_pg, connection_id="warehouse_pg")
    assert isinstance(connector, PostgresConnector)


def test_connector_by_id_unknown(config_with_pg: CanonConfig) -> None:
    with pytest.raises(ConnectionError, match="unknown connection 'nonexistent'"):
        connector_by_id(config_with_pg, connection_id="nonexistent")


def test_connector_by_id_no_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PG_PASSWORD", "pw")
    config = CanonConfig.model_validate(
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
        connector_by_id(config, connection_id=None)
