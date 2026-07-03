"""Connector factory: type-name registry and instantiation (SPEC-E2 §2.2a, S9).

The core dispatches on a connection's declared ``type`` via :class:`ConnectorFactory` —
the single place vendor-name dispatch happens.  Downstream epics (E3, etc.) register new
types by calling ``default_factory.register()`` without touching core logic (AC4).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from canonic.connectors.dbt import DbtConnector
from canonic.connectors.duckdb import DuckDBConnector
from canonic.connectors.evidence import GenericEvidenceConnector
from canonic.connectors.looker import LookerConnector
from canonic.connectors.metabase import MetabaseConnector
from canonic.connectors.notion import DEFAULT_API_VERSION as _NOTION_DEFAULT_API_VERSION
from canonic.connectors.notion import make_notion_connector
from canonic.connectors.postgres import PostgresConnector
from canonic.connectors.redshift import RedshiftConnector
from canonic.connectors.sqlite import SQLiteConnector
from canonic.exc import ConnectionError, UnknownConnectorType

if TYPE_CHECKING:
    from collections.abc import Callable

    from canonic.config import CanonicConfig, Connection
    from canonic.connectors.base import ConnectorBase

__all__ = ["ConnectorFactory", "default_factory"]


def _make_dbt(conn: Connection) -> DbtConnector:
    manifest_path = conn.params.get("manifest_path", "manifest.json")
    return DbtConnector(manifest_path, source=conn.id)


def _make_notion(conn: Connection) -> GenericEvidenceConnector:
    from canonic.credentials import resolve_credential

    token = resolve_credential(conn.credentials_ref)
    api_version = conn.params.get("api_version", _NOTION_DEFAULT_API_VERSION)
    return make_notion_connector(token, source=conn.id, api_version=api_version)


class ConnectorFactory:
    """The single place connection ``type`` → connector dispatch happens (E2 §2.2a).

    Register a builder (a connector class whose ``__init__`` takes a
    :class:`~canonic.config.Connection`, or an adapter function) via :meth:`register`.
    Instantiate by type via :meth:`instantiate`, or pass the full connection via
    :meth:`create`.  A module-level :data:`default_factory` singleton holds the builtin
    registry; downstream epics add types there without touching core logic (AC4).
    """

    def __init__(self) -> None:
        self._registry: dict[str, Callable[[Connection], ConnectorBase]] = {}

    def register(self, type_name: str, builder: Callable[[Connection], ConnectorBase]) -> None:
        """Register a builder for ``type_name``.

        ``builder`` receives a :class:`~canonic.config.Connection` and returns a connector.
        A connector class whose ``__init__`` takes a ``Connection`` qualifies directly;
        file-based or credential-unpacking connectors use a thin adapter function.
        Later calls for the same name replace the prior registration.
        """
        self._registry[type_name] = builder

    def instantiate(self, type_name: str, connection: Connection) -> ConnectorBase:
        """Build a connector for ``type_name`` from ``connection``.

        Raises :class:`~canonic.exc.UnknownConnectorType` (exit 13) when the type is not
        registered — no silent fallback (AC2).
        """
        builder = self._registry.get(type_name)
        if builder is None:
            raise UnknownConnectorType(type_name, known=sorted(self._registry))
        return builder(connection)

    def create(self, connection: Connection) -> ConnectorBase:
        """Build a connector for ``connection`` using its own declared ``type``.

        Convenience wrapper around :meth:`instantiate`.
        """
        return self.instantiate(connection.type, connection)

    def for_id(self, config: CanonicConfig, connection_id: str | None) -> ConnectorBase:
        """Resolve a connection by id (or the project default) and build its connector.

        ``connection_id=None`` falls back to ``config.project.default_connection``.
        Raises :class:`~canonic.exc.ConnectionError` (exit 13) when no connection can be
        selected or the named connection is unknown; :class:`~canonic.exc.UnknownConnectorType`
        (also exit 13) when the resolved connection has no registered type.
        """
        target = connection_id or config.project.default_connection
        if target is None:
            raise ConnectionError(
                "no connection specified and project has no default_connection configured"
            )
        for conn in config.connections:
            if conn.id == target:
                return self.create(conn)
        known = ", ".join(c.id for c in config.connections) or "(none)"
        raise ConnectionError(f"unknown connection {target!r}; configured: {known}")

    def registered_types(self) -> list[str]:
        """Return sorted list of registered type names."""
        return sorted(self._registry)


def _build_default_factory() -> ConnectorFactory:
    factory = ConnectorFactory()
    factory.register("dbt", _make_dbt)
    factory.register("duckdb", DuckDBConnector)
    factory.register("looker", LookerConnector)
    factory.register("metabase", MetabaseConnector)
    factory.register("notion", _make_notion)
    factory.register("postgres", PostgresConnector)
    factory.register("redshift", RedshiftConnector)
    factory.register("sqlite", SQLiteConnector)
    return factory


# Builtin registry — CLI/daemon import this singleton.  AC4: a new connector adds one
# register() call here without touching any core logic.
default_factory = _build_default_factory()
