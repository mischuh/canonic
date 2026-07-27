"""Tests for the connector factory (SPEC-E2 §2.2a) — dbt's target_connection wiring."""

from __future__ import annotations

from typing import TYPE_CHECKING

from canonic.config import Connection
from canonic.connectors.dbt import DbtConnector
from canonic.connectors.factory import default_factory

if TYPE_CHECKING:
    from pathlib import Path


class TestMakeDbt:
    def test_target_connection_stamps_physical_connection_id(self, dbt_manifest_path: Path) -> None:
        conn = Connection(
            id="jaffle_dbt",
            type="dbt",
            params={"manifest_path": str(dbt_manifest_path), "target_connection": "jaffle_duckdb"},
        )
        connector = default_factory.create(conn)
        assert isinstance(connector, DbtConnector)
        assert connector._source == "jaffle_duckdb"  # noqa: SLF001 — verifying the wiring itself

    def test_missing_target_connection_falls_back_to_own_id(self, dbt_manifest_path: Path) -> None:
        conn = Connection(
            id="jaffle_dbt", type="dbt", params={"manifest_path": str(dbt_manifest_path)}
        )
        connector = default_factory.create(conn)
        assert isinstance(connector, DbtConnector)
        assert connector._source == "jaffle_dbt"  # noqa: SLF001 — today's default behavior
