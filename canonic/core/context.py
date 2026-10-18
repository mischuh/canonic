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
from contextlib import contextmanager
from contextvars import ContextVar
from typing import TYPE_CHECKING, cast

from canonic.connectors.base import Capability, require_capability
from canonic.connectors.factory import default_factory
from canonic.contracts.principal import Principal
from canonic.contracts.resolver import Ambiguous as ResolverAmbiguous
from canonic.contracts.resolver import Unresolved as ResolverUnresolved
from canonic.core.probes import strip_probes
from canonic.core.project import ProjectState
from canonic.exc import Ambiguous, Unresolved

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from canonic.compiler.result import CompileResult
    from canonic.config import CanonicConfig
    from canonic.connectors.base import ConnectorBase, ResultSet, SQLExecutable
    from canonic.contracts.resolver import Binding, ContractResolver
    from canonic.core.probes import ProbeValues
    from canonic.core.project import ProjectReloader
    from canonic.instrumentation.events import AnswerEventLog
    from canonic.semantic.models import SemanticSource

_ANONYMOUS_PRINCIPAL = Principal()

logger = logging.getLogger(__name__)

#: The project state a request in flight was started on, with the context that owns it.
_PINNED: ContextVar[tuple[ServiceContext, ProjectState] | None] = ContextVar(
    "canonic_pinned_project_state", default=None
)


class ServiceContext:
    """The shared state and infrastructure the core service collaborators build on.

    The project definitions (config, contracts, semantic sources) live in one immutable
    :class:`ProjectState`. A context built with a :class:`ProjectReloader` swaps that state when
    the project files change, and :meth:`request` pins the state for the duration of a request so
    the request finishes on the definitions it started with.
    """

    def __init__(
        self,
        *,
        config: CanonicConfig,
        resolver: ContractResolver,
        sources: list[SemanticSource],
        connection_dialects: dict[str, str],
        project_root: Path | None,
        event_log: AnswerEventLog,
        reloader: ProjectReloader | None = None,
    ) -> None:
        self._state = ProjectState(
            config=config,
            resolver=resolver,
            sources=sources,
            connection_dialects=connection_dialects,
            # name → source for fast lookup (sources have unique names within project)
            source_by_name={s.name: s for s in sources},
        )
        self._reloader = reloader
        self.project_root = project_root
        self.event_log = event_log

    @property
    def state(self) -> ProjectState:
        """The pinned state inside a request, otherwise the latest loaded one."""
        pinned = _PINNED.get()
        if pinned is not None and pinned[0] is self:
            return pinned[1]
        return self._reloader.state if self._reloader is not None else self._state

    @property
    def config(self) -> CanonicConfig:
        return self.state.config

    @property
    def resolver(self) -> ContractResolver:
        return self.state.resolver

    @property
    def sources(self) -> list[SemanticSource]:
        return self.state.sources

    @property
    def connection_dialects(self) -> dict[str, str]:
        return self.state.connection_dialects

    @property
    def source_by_name(self) -> dict[str, SemanticSource]:
        return self.state.source_by_name

    @property
    def reload_warning(self) -> str | None:
        """Why the project files on disk are not being served, or ``None`` when they are."""
        failure = self._reloader.failure if self._reloader is not None else None
        return failure.warning() if failure is not None else None

    @contextmanager
    def request(self) -> Iterator[ProjectState]:
        """Serve one request on a single project state, reloading first if the files changed.

        Nested calls, such as a report running its sections through ``query``, reuse the state
        the outermost call pinned, so a reload never lands in the middle of a request.
        """
        pinned = _PINNED.get()
        if pinned is not None and pinned[0] is self:
            yield pinned[1]
            return
        state = self._reloader.refresh() if self._reloader is not None else self._state
        token = _PINNED.set((self, state))
        try:
            yield state
        finally:
            _PINNED.reset(token)

    def resolve_or_raise(
        self, name: str, context: str | None = None, *, principal: Principal | None = None
    ) -> Binding:
        """Resolve *name* to a :class:`Binding`, mapping resolver results onto coded exceptions.

        Raises :class:`canonic.exc.Unresolved` (exit 2) or :class:`canonic.exc.Ambiguous`
        (exit 3) — the headless error codes the CLI/MCP adapters surface.

        A name outside *principal*'s effective policy also raises :class:`Unresolved`, with
        the identical message shape a nonexistent name produces — reusing the compiler's
        stage 1 divergence (SPEC-E12 §3) so ``describe_metric``/``resolve_metric`` never turn
        into an existence oracle for metrics the caller is not entitled to know about (S15
        AC3). Checked *before* resolution, mirroring the compiler's ordering. ``principal is
        None`` still applies whatever ``default_role``/deny-by-default the project's role
        policy declares for an unauthenticated caller — matching :func:`compile`'s stage 0,
        which never treats a missing principal as an unrestricted one once a policy is loaded.
        """
        bound = principal if principal is not None else _ANONYMOUS_PRINCIPAL
        if not self.resolver.authz_for(bound).metric_allowed(name):
            raise Unresolved(f"metric {name!r} matches no active binding")
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

    async def execute_compiled(
        self, compiled: CompileResult, connection_id: str | None
    ) -> tuple[ResultSet, ProbeValues]:
        """Run a compiled query and return its result without the compiler's probe columns.

        The second value holds what each probe counted, for the response metadata.
        """
        return strip_probes(compiled, await self.execute(compiled.sql, connection_id))

    def connection_for_sql(self, compiled: CompileResult) -> str | None:
        """Pick the connection id from the first resolved metric's owning source.

        A resolved key is ``source.measure``, or wraps it in its kind, such as
        ``cumulative(source.measure)``. The wrapper is stripped before the source is read.
        """
        for source_measure in compiled.resolved.values():
            _, _, inner = source_measure.rpartition("(")
            source_name = inner.split(".")[0]
            source = self.source_by_name.get(source_name)
            if source is not None:
                return source.connection
        logger.warning(
            "no source-level connection match: resolved=%s"
            " — falling back to project default_connection",
            list(compiled.resolved.values()),
        )
        return None
