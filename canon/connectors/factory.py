"""Connection-id → connector resolution (SPEC-E2 §6, SPEC-E7-E8 §2).

The core dispatches on a connection's declared ``type`` via a small registry,
never on vendor identity at the call site. Phase 0 registers only PostgreSQL.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from canon.connectors.base import ConnectorBase  # noqa: TC001 — used in _REGISTRY annotation
from canon.connectors.looker import LookerConnector
from canon.connectors.metabase import MetabaseConnector
from canon.connectors.postgres import PostgresConnector
from canon.exc import ConnectionError

if TYPE_CHECKING:
    from canon.config import CanonConfig, Connection

__all__ = ["connector_by_id", "connector_for"]

# Declared connection type → connector class. New primary connectors register here.
# The value type is intentionally broad so the factory can call cls(conn) uniformly.
_REGISTRY: dict[str, type] = {
    "looker": LookerConnector,
    "metabase": MetabaseConnector,
    "postgres": PostgresConnector,
}


def connector_for(conn: Connection) -> ConnectorBase:
    """Build a connector for a single :class:`Connection` by its declared type.

    Raises :class:`ConnectionError` (exit 13) when the type has no registered
    connector.
    """
    connector_cls = _REGISTRY.get(conn.type)
    if connector_cls is None:
        known = ", ".join(sorted(_REGISTRY)) or "(none)"
        raise ConnectionError(
            f"connection {conn.id!r} has unsupported type {conn.type!r}; known types: {known}"
        )
    instance: ConnectorBase = connector_cls(conn)
    return instance


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
