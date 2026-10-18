"""Protocol-neutral capability layer — the single implementation of all core capabilities.

MCP and CLI adapters call this service; they do not duplicate any logic (SPEC §2.1).

:class:`CanonicService` is a thin facade: it wires the injected dependencies into a shared
:class:`~canonic.core.context.ServiceContext` and delegates each capability to one of five
focused collaborators — :class:`~canonic.core.discovery.DiscoveryService`,
:class:`~canonic.core.query.QueryService`, :class:`~canonic.core.assertions.AssertionService`,
:class:`~canonic.core.knowledge.KnowledgeService`, and :class:`~canonic.core.reports.ReportService`.
The public method surface is unchanged, so the CLI/MCP adapters and their byte-identical parity
are unaffected.
"""

from __future__ import annotations

import functools
from typing import TYPE_CHECKING, Any

from canonic.core.assertions import AssertionService
from canonic.core.context import ServiceContext
from canonic.core.discovery import DiscoveryService
from canonic.core.knowledge import KnowledgeService
from canonic.core.project import ProjectInfo, ProjectReloader, ProjectState
from canonic.core.query import QueryService
from canonic.core.reports import ReportService
from canonic.instrumentation.events import DiskAnswerEventLog, NullAnswerEventLog

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine
    from datetime import datetime
    from pathlib import Path

    from canonic.compiler import SemanticQuery
    from canonic.compiler.result import CompileResult
    from canonic.config import CanonicConfig, ProjectReload
    from canonic.connectors.base import ResultSet
    from canonic.contracts import ContractResolver
    from canonic.contracts.assertions import AccuracyReport, AssertionOutcome
    from canonic.contracts.models import Assertion
    from canonic.contracts.principal import Caller, Principal
    from canonic.contracts.resolver import Binding
    from canonic.core.models import (
        MetricDetail,
        MetricSummary,
        OverviewResult,
        QueryResult,
        ReportRunResult,
        ReportSummary,
    )
    from canonic.instrumentation.events import AnswerEventLog
    from canonic.knowledge.results import SearchResult
    from canonic.semantic.models import SemanticSource
    from canonic.trust.models import TrustScore

__all__ = ["CanonicService"]


def _served[**P, R](method: Callable[P, R]) -> Callable[P, R]:
    """Run a public method on one project state, reloading first if the files changed."""

    @functools.wraps(method)
    def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
        service: CanonicService = args[0]  # type: ignore[assignment]
        with service._ctx.request():
            return method(*args, **kwargs)

    return wrapper


def _served_async[**P, R](
    method: Callable[P, Coroutine[Any, Any, R]],
) -> Callable[P, Coroutine[Any, Any, R]]:
    """:func:`_served` for a coroutine method: the state stays pinned until it finishes."""

    @functools.wraps(method)
    async def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
        service: CanonicService = args[0]  # type: ignore[assignment]
        with service._ctx.request():
            return await method(*args, **kwargs)

    return wrapper


class CanonicService:
    """Capability layer loaded once per daemon/process (SPEC §2, §4).

    ``from_project`` is the normal entry point; tests can construct directly. Every capability
    is delegated to a focused collaborator; this class only owns construction and delegation.
    """

    def __init__(
        self,
        config: CanonicConfig,
        resolver: ContractResolver,
        sources: list[SemanticSource],
        *,
        project_root: Path | None = None,
        event_log: AnswerEventLog | None = None,
        reloader: ProjectReloader | None = None,
    ) -> None:
        self._event_log: AnswerEventLog = (
            event_log if event_log is not None else NullAnswerEventLog()
        )
        state = ProjectState.build(config, resolver, sources)
        self._ctx = ServiceContext(
            config=config,
            resolver=resolver,
            sources=sources,
            connection_dialects=state.connection_dialects,
            project_root=project_root,
            event_log=self._event_log,
            reloader=reloader,
        )
        ctx = self._ctx
        self._discovery = DiscoveryService(ctx)
        self._assertions = AssertionService(ctx)
        self._knowledge = KnowledgeService(ctx)
        self._query = QueryService(ctx, self._assertions, self._knowledge)
        self._reports = ReportService(ctx, self._query, self._knowledge)

    @property
    def resolver(self) -> ContractResolver:
        """The project's :class:`ContractResolver` (SPEC-E12 §2 — tenancy/role policies).

        Exposed for adapters (``canonic.mcp.server``, ``canonic.mcp.daemon``) that need
        to derive a :class:`~canonic.contracts.principal.Principal` from a verified token
        or check ``tenancy_enabled`` before serving, without duplicating resolver wiring.
        """
        return self._ctx.resolver

    @_served
    def project_info(self) -> ProjectInfo:
        """The fingerprint and load time of the project state this service is serving.

        Checks the project files first, so the answer is the state the next request will use.
        Both are ``None`` for a service built in memory, which has no files.
        """
        state = self._ctx.state
        return ProjectInfo(
            fingerprint=state.fingerprint,
            loaded_at=state.loaded_at,
            files_changed=self._ctx.project_files_changed,
            reload=self._ctx.reload_mode,
        )

    @classmethod
    def from_project(cls, root: Path, *, reload: ProjectReload | None = None) -> CanonicService:
        """Load config, resolver, and semantic sources from a project root.

        ``reload`` overrides ``project.reload`` from ``canonic.yaml``. Under ``off`` (the
        default) the service serves the state it loaded until the process restarts, and
        :meth:`project_info` reports when the files on disk have moved on. Under ``auto``,
        before each request it reloads ``canonic.yaml``, ``contracts/`` and ``semantics/`` if
        one of them changed, and keeps serving the previous state when the new files do not
        validate. A service constructed directly has no files to follow and serves what it
        was given.
        """
        state = ProjectState.load(root)
        return cls(
            config=state.config,
            resolver=state.resolver,
            sources=state.sources,
            project_root=root,
            event_log=DiskAnswerEventLog(root, state.config.instrumentation.events),
            reloader=ProjectReloader(
                root, state, reload if reload is not None else state.config.project.reload
            ),
        )

    # ------------------------------------------------------------------
    # Discovery
    # ------------------------------------------------------------------

    @_served
    def list_metrics(self, *, principal: Principal | None = None) -> list[MetricSummary]:
        """Return a summary of every active canonical metric (SPEC §4.1).

        Omits every metric outside ``principal``'s effective policy (SPEC-E12 §6, S15 AC1).
        """
        return self._discovery.list_metrics(principal=principal)

    @_served
    def trust_report(self) -> list[tuple[str, TrustScore]]:
        """Static trust tier for every active canonical metric, sorted by name (SPEC-E14 §8)."""
        return self._discovery.trust_report()

    @_served
    def describe_metric(self, name: str, *, principal: Principal | None = None) -> MetricDetail:
        """Return grain, dimensions, measures, and freshness for a metric (SPEC §4.1).

        Raises the same ``Unresolved`` shape for a name outside ``principal``'s effective
        policy as for a nonexistent name (SPEC-E12 §3, S15 AC3).
        """
        return self._discovery.describe_metric(name, principal=principal)

    @_served
    def get_overview(
        self, domain: str | None = None, *, principal: Principal | None = None
    ) -> OverviewResult:
        """Return active metrics grouped by domain with sample questions (S12).

        Omits every metric outside ``principal``'s effective policy (SPEC-E12 §6, S15 AC1).
        """
        return self._discovery.get_overview(domain, principal=principal)

    # ------------------------------------------------------------------
    # Query
    # ------------------------------------------------------------------

    @_served
    def resolve_metric(
        self, name: str, context: str | None = None, *, principal: Principal | None = None
    ) -> Binding:
        """Resolve a metric name and return the :class:`Binding` (raises on unresolved/ambiguous).

        Raises the same ``Unresolved`` shape for a name outside ``principal``'s effective
        policy as for a nonexistent name (SPEC-E12 §3, S15 AC3).
        """
        return self._query.resolve_metric(name, context=context, principal=principal)

    @_served
    def compile_query(
        self, query: SemanticQuery, *, principal: Principal | None = None
    ) -> CompileResult:
        """Compile a semantic query to SQL + metadata with no execution (SPEC §2).

        ``principal`` is bound by the adapter from a verified token/CLI override, never
        from ``query`` itself, and drives the compiler's tenant/role scoping (SPEC-E12).
        """
        return self._query.compile_query(query, principal=principal)

    @_served_async
    async def query(
        self,
        query: SemanticQuery,
        *,
        harness: bool = False,
        caller: Caller | None = None,
        principal: Principal | None = None,
    ) -> QueryResult:
        """Compile and execute a semantic query read-only (SPEC §2)."""
        return await self._query.query(query, harness=harness, caller=caller, principal=principal)

    @_served_async
    async def run_sql(
        self,
        sql: str,
        connection: str | None = None,
        *,
        caller: Caller | None = None,
        principal: Principal | None = None,
    ) -> ResultSet:
        """Execute a raw read-only SQL string on the named connection (SPEC §2).

        Raises :class:`~canonic.exc.TenantForbidden` when ``principal``'s role denies raw
        SQL, or when tenancy is enabled and the connection carries no ``rls_enforced: true``
        attestation (SPEC-E12 §6).
        """
        return await self._query.run_sql(sql, connection, caller=caller, principal=principal)

    # ------------------------------------------------------------------
    # Assertions (SPEC-Fuller-E15 §3) — the oracle for E16's accuracy harness
    # ------------------------------------------------------------------

    @_served_async
    async def run_assertion(
        self, assertion: Assertion, *, resolver: ContractResolver | None = None
    ) -> AssertionOutcome:
        """Compile, execute read-only, and match one assertion (SPEC-Fuller-E15 §3.2)."""
        return await self._assertions.run_assertion(assertion, resolver=resolver)

    @_served_async
    async def check_assertions(
        self, assertions: list[Assertion] | None = None
    ) -> list[AssertionOutcome]:
        """Run every executable assertion and return its outcome (SPEC-Fuller-E15 §3.4)."""
        return await self._assertions.check_assertions(assertions)

    @_served_async
    async def run_accuracy_harness(
        self, assertions: list[Assertion] | None = None
    ) -> AccuracyReport:
        """Run the labeled assertion set and compute its accuracy (SPEC-Fuller-E15 §3.4)."""
        return await self._assertions.run_accuracy_harness(assertions)

    @_served_async
    async def run_accuracy_baseline(
        self, assertions: list[Assertion] | None = None
    ) -> AccuracyReport:
        """Run the labeled assertion set against a schema-only resolver (SPEC-E16 Part 2 §2)."""
        return await self._assertions.run_accuracy_baseline(assertions)

    # ------------------------------------------------------------------
    # Knowledge (E6, P1)
    # ------------------------------------------------------------------

    @_served
    def search_knowledge(
        self,
        query: str,
        *,
        user: str | None = None,
        limit: int = 10,
        principal: Principal | None = None,
    ) -> SearchResult:
        """Search knowledge pages for business context (E6, P1).

        ``principal``'s role ``knowledge.allow_tags`` filters the searchable corpus, on top
        of the existing global/user-scope visibility ``user`` governs (SPEC-E12 §6, S15 AC2).
        """
        return self._knowledge.search_knowledge(query, user=user, limit=limit, principal=principal)

    @_served
    def read_knowledge_page(
        self, page: str, *, user: str | None = None, principal: Principal | None = None
    ) -> dict[str, Any]:
        """Retrieve the full content of a knowledge page with live rendering (E6, P1)."""
        return self._knowledge.read_knowledge_page(page, user=user, principal=principal)

    # ------------------------------------------------------------------
    # Reports (AMENDMENT-curated-reports, P1)
    # ------------------------------------------------------------------

    @_served
    def list_reports(self, domain: str | None = None) -> list[ReportSummary]:
        """Directory listing of committed reports (AMENDMENT-curated-reports)."""
        return self._reports.list_reports(domain)

    @_served_async
    async def run_report(
        self,
        report_id: str,
        *,
        as_of: datetime | None = None,
        filters: list[str] | None = None,
        user: str | None = None,
        caller: Caller | None = None,
        principal: Principal | None = None,
    ) -> ReportRunResult:
        """Run every section of a committed report through ``query``, in order.

        ``principal`` flows into every section's ``query()`` call; caller-supplied
        ``filters`` narrow but never widen it (SPEC-E12 §5, S14 AC1).
        """
        return await self._reports.run_report(
            report_id,
            as_of=as_of,
            filters=filters,
            user=user,
            caller=caller,
            principal=principal,
        )

    @_served
    def validate_reports(self) -> None:
        """Validate every committed report's sections compile and narrative refs resolve."""
        self._reports.validate_reports()
