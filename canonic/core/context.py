"""Shared dependencies and infrastructure helpers for the core service collaborators.

:class:`CanonicService` is a thin facade (see :mod:`canonic.core.service`) over four focused
collaborators — discovery, query, assertions, knowledge. They all need the same injected
dependencies (config, resolver, sources, event log) and a few shared infrastructure helpers
(resolve-or-raise, read-only SQL execution, connection selection). This context object holds
them in one place so each collaborator depends on the context rather than on the others or on
a duplicated copy of the wiring.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, cast

from canonic.connectors.base import Capability, require_capability
from canonic.connectors.factory import default_factory
from canonic.contracts.resolver import Ambiguous as ResolverAmbiguous
from canonic.contracts.resolver import Unresolved as ResolverUnresolved
from canonic.exc import Ambiguous, Unresolved

if TYPE_CHECKING:
    from pathlib import Path

    from canonic.compiler.result import CompileResult
    from canonic.config import CanonicConfig
    from canonic.connectors.base import ConnectorBase, ResultSet, SQLExecutable
    from canonic.contracts.resolver import Binding, ContractResolver
    from canonic.instrumentation.events import AnswerEventLog
    from canonic.semantic.models import SemanticSource

logger = logging.getLogger(__name__)


class ServiceContext:
    """The shared state and infrastructure the core service collaborators build on."""

    def __init__(
        self,
        *,
        config: CanonicConfig,
        resolver: ContractResolver,
        sources: list[SemanticSource],
        connection_dialects: dict[str, str],
        project_root: Path | None,
        event_log: AnswerEventLog,
    ) -> None:
        self.config = config
        self.resolver = resolver
        self.sources = sources
        self.connection_dialects = connection_dialects
        self.project_root = project_root
        self.event_log = event_log
        # name → source for fast lookup (sources have unique names within project)
        self.source_by_name: dict[str, SemanticSource] = {s.name: s for s in sources}

    def resolve_or_raise(self, name: str, context: str | None = None) -> Binding:
        """Resolve *name* to a :class:`Binding`, mapping resolver results onto coded exceptions.

        Raises :class:`canonic.exc.Unresolved` (exit 2) or :class:`canonic.exc.Ambiguous`
        (exit 3) — the headless error codes the CLI/MCP adapters surface.
        """
        result = self.resolver.resolve_metric(name, context=context)
        if isinstance(result, ResolverUnresolved):
            raise Unresolved(f"metric {name!r} matches no active binding")
        if isinstance(result, ResolverAmbiguous):
            raise Ambiguous(
                f"metric {name!r} is ambiguous",
                candidates=list(result.candidates),
            )
        return result

    def connector_for(self, connection: str | None) -> ConnectorBase:
        """Open the connector for a connection id; the caller owns closing it.

        The single place ``default_factory`` is consulted at runtime, so both the semantic
        query path and the raw-SQL escape hatch resolve connectors identically.
        """
        return default_factory.for_id(self.config, connection)

    async def execute(self, sql: str, connection_id: str | None) -> ResultSet:
        """Run read-only SQL on the resolved connection, always closing the connector."""
        connector = self.connector_for(connection_id)
        try:
            return await cast(
                "SQLExecutable", require_capability(connector, Capability.RUN_READ_ONLY_SQL)
            ).run_read_only_sql(sql)
        finally:
            await connector.aclose()

    def connection_for_sql(self, compiled: CompileResult) -> str | None:
        """Pick the connection id from the first resolved metric's owning source."""
        for source_measure in compiled.resolved.values():
            source_name = source_measure.split(".")[0]
            source = self.source_by_name.get(source_name)
            if source is not None:
                return source.connection
        logger.warning(
            "no source-level connection match: resolved=%s"
            " — falling back to project default_connection",
            list(compiled.resolved.values()),
        )
        return None
