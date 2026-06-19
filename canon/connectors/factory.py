"""Connection-id → connector resolution (SPEC-E2 §6, SPEC-E7-E8 §2).

The core dispatches on a connection's declared ``type`` via a small registry,
never on vendor identity at the call site.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from canon.connectors.dbt import DbtConnector
from canon.connectors.looker import LookerConnector
from canon.connectors.metabase import MetabaseConnector
from canon.connectors.notion import DEFAULT_API_VERSION as _NOTION_DEFAULT_API_VERSION
from canon.connectors.notion import NotionConnector
from canon.connectors.postgres import PostgresConnector
from canon.exc import ConnectionError

if TYPE_CHECKING:
    from collections.abc import Callable

    from canon.config import CanonConfig, Connection
    from canon.connectors.base import ConnectorBase

__all__ = ["connector_by_id", "connector_for"]


def _make_dbt(conn: Connection) -> DbtConnector:
    manifest_path = conn.params.get("manifest_path", "manifest.json")
    return DbtConnector(manifest_path, source=conn.id)


def _make_notion(conn: Connection) -> NotionConnector:
    from canon.credentials import resolve_credential

    token = resolve_credential(conn.credentials_ref)
    api_version = conn.params.get("api_version", _NOTION_DEFAULT_API_VERSION)
    return NotionConnector(token, source=conn.id, api_version=api_version)


# Declared connection type → factory callable. New connectors register here.
_REGISTRY: dict[str, Callable[[Connection], ConnectorBase]] = {
    "dbt": _make_dbt,
    "looker": LookerConnector,
    "metabase": MetabaseConnector,
    "notion": _make_notion,
    "postgres": PostgresConnector,
}


def connector_for(conn: Connection) -> ConnectorBase:
    """Build a connector for a single :class:`Connection` by its declared type.

    Raises :class:`ConnectionError` (exit 13) when the type has no registered
    connector.
    """
    factory = _REGISTRY.get(conn.type)
    if factory is None:
        known = ", ".join(sorted(_REGISTRY)) or "(none)"
        raise ConnectionError(
            f"connection {conn.id!r} has unsupported type {conn.type!r}; known types: {known}"
        )
    return factory(conn)


def connector_by_id(config: CanonConfig, connection_id: str | None) -> ConnectorBase:
    """Resolve a connection by id (or the project default) and build its connector.

    ``connection_id=None`` falls back to ``config.project.default_connection``.
    Raises :class:`ConnectionError` (exit 13) when no connection can be selected
    or the named connection is unknown.
    """
    target = connection_id or config.project.default_connection
    if target is None:
        raise ConnectionError(
            "no connection specified and project has no default_connection configured"
        )
    for conn in config.connections:
        if conn.id == target:
            return connector_for(conn)
    known = ", ".join(c.id for c in config.connections) or "(none)"
    raise ConnectionError(f"unknown connection {target!r}; configured: {known}")
