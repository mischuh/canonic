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

from pathlib import Path
from typing import TYPE_CHECKING

from canonic.compiler.dialect import DIALECT_ADAPTERS, adapter_for
from canonic.config import CanonicConfig, Connection, load_config
from canonic.contracts import ContractResolver
from canonic.core.assertions import AssertionService
from canonic.core.context import ServiceContext
from canonic.core.discovery import DiscoveryService, domain_for_metric
from canonic.core.gitwrite import FilesystemContentWriter, GitContentWriter
from canonic.core.knowledge import KnowledgeService
from canonic.core.models import ReportRef
from canonic.core.query import QueryService
from canonic.core.reports import ReportService
from canonic.core.saved import SavedContentService
from canonic.instrumentation.events import AnswerEventLog, DiskAnswerEventLog, NullAnswerEventLog
from canonic.semantic.loader import list_semantic_sources

if TYPE_CHECKING:
    from datetime import datetime
    from typing import Any

    from canonic.compiler import SemanticQuery
    from canonic.compiler.result import CompileResult
    from canonic.connectors.base import ResultSet
    from canonic.contracts.assertions import AccuracyReport, AssertionOutcome
    from canonic.contracts.models import Assertion
    from canonic.contracts.principal import Principal
    from canonic.contracts.resolver import Binding
    from canonic.core.gitwrite import ContentWriter
    from canonic.core.models import (
        MetricDetail,
        MetricSummary,
        OverviewResult,
        QueryResult,
        ReportRunResult,
        ReportStructure,
        ReportSummary,
        SavedQuerySummary,
    )
    from canonic.knowledge.results import SearchResult
    from canonic.semantic.models import SemanticSource
    from canonic.trust.models import TrustScore

__all__ = ["CanonicService"]


_TYPE_ALIASES: dict[str, str] = {
    "postgresql": "postgres",
    "pg": "postgres",
}


def _dialect_for_type(connector_type: str) -> str | None:
    """Map a query connector's type string to a sqlglot dialect name.

    Returns ``None`` for a definitions/evidence-only connector type (dbt, looker,
    metabase, notion, url) — those never serve a compiled query, so they get no entry
    in ``connection_dialects`` at all rather than a meaningless placeholder dialect.
    A project pairing a query connector with, say, a dbt companion connection (see
    examples/dutch-railway, jaffle-shop, ecommerce) is the normal case this must not
    break: only the query connector's type reaches :func:`adapter_for`.
    """
    dialect = _TYPE_ALIASES.get(connector_type, connector_type)
    if dialect not in DIALECT_ADAPTERS:
        return None
    return adapter_for(dialect).dialect


_FILE_PATH_PARAMS: dict[str, str] = {
    "duckdb": "path",
    "sqlite": "path",
    "dbt": "manifest_path",
}


def _resolve_connection_paths(connections: list[Connection], root: Path) -> None:
    """Resolve relative file paths in file-based connections against the project root.

    Mutates params in-place so callers downstream always receive absolute paths,
    regardless of the process working directory.
    """
    for conn in connections:
        param_key = _FILE_PATH_PARAMS.get(conn.type)
        if param_key is None:
            continue
        raw = conn.params.get(param_key)
        if raw and not Path(raw).is_absolute():
            conn.params[param_key] = str(root / raw)


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
    ) -> None:
        self._config = config
        self._resolver = resolver
        self._sources = sources
        self._project_root = project_root
        self._event_log: AnswerEventLog = (
            event_log if event_log is not None else NullAnswerEventLog()
        )
        # connection id → sqlglot dialect name, derived from connection types in config.
        # Definitions/evidence-only connections (dbt, looker, ...) get no entry — see
        # _dialect_for_type.
        connection_dialects: dict[str, str] = {
            c.id: dialect
            for c in config.connections
            if (dialect := _dialect_for_type(c.type)) is not None
        }
        ctx = ServiceContext(
            config=config,
            resolver=resolver,
            sources=sources,
            connection_dialects=connection_dialects,
            project_root=project_root,
            event_log=self._event_log,
        )
        self._discovery = DiscoveryService(ctx)
        self._assertions = AssertionService(ctx)
        self._query = QueryService(ctx, self._assertions)
        self._knowledge = KnowledgeService(ctx)
        self._reports = ReportService(ctx, self._query, self._knowledge)
        writer: ContentWriter = (
            GitContentWriter(project_root)
            if project_root is not None
            else FilesystemContentWriter()
        )
        self._saved = SavedContentService(ctx, self._query, writer)

    @property
    def resolver(self) -> ContractResolver:
        """The project's :class:`ContractResolver` (SPEC-E12 §2 — tenancy/role policies).

        Exposed for adapters (``canonic.mcp.server``, ``canonic.mcp.daemon``) that need
        to derive a :class:`~canonic.contracts.principal.Principal` from a verified token
        or check ``tenancy_enabled`` before serving, without duplicating resolver wiring.
        """
        return self._resolver

    @classmethod
    def from_project(cls, root: Path) -> CanonicService:
        """Load config, resolver, and semantic sources from a project root."""
        config = load_config(root / "canonic.yaml")
        _resolve_connection_paths(config.connections, root)
        resolver = ContractResolver.from_project(root)
        sources = list_semantic_sources(root)
        return cls(
            config=config,
            resolver=resolver,
            sources=sources,
            project_root=root,
            event_log=DiskAnswerEventLog(root),
        )

    # ------------------------------------------------------------------
    # Discovery
    # ------------------------------------------------------------------

    def list_metrics(self, *, principal: Principal | None = None) -> list[MetricSummary]:
        """Return a summary of every active canonical metric (SPEC §4.1).

        Omits every metric outside ``principal``'s effective policy (SPEC-E12 §6, S15 AC1).
        """
        return self._discovery.list_metrics(principal=principal)

    def trust_report(self) -> list[tuple[str, TrustScore]]:
        """Static trust tier for every active canonical metric, sorted by name (SPEC-E14 §8)."""
        return self._discovery.trust_report()

    def describe_metric(self, name: str, *, principal: Principal | None = None) -> MetricDetail:
        """Return grain, dimensions, measures, and freshness for a metric (SPEC §4.1).

        Raises the same ``Unresolved`` shape for a name outside ``principal``'s effective
        policy as for a nonexistent name (SPEC-E12 §3, S15 AC3).
        """
        return self._discovery.describe_metric(name, principal=principal)

    def get_overview(
        self,
        domain: str | None = None,
        *,
        user: str | None = None,
        principal: Principal | None = None,
    ) -> OverviewResult:
        """Return active metrics grouped by domain with sample questions (S12).

        Omits every metric outside ``principal``'s effective policy (SPEC-E12 §6, S15 AC1).

        ``reports`` lists every report visible to ``user`` (``global/`` + their own scope,
        never ``queries/``), each tagged with its scope (S24 AC2). When ``user`` is given, a
        saved query carrying a ``question`` feeds its domain's ``sample_questions`` — it is not
        itself listed under ``reports`` (S24 AC3).
        """
        report_refs = [
            ReportRef(id=s.id, title=s.title, scope=s.scope)
            for s in self._reports.list_reports(domain, user=user)
        ]
        extra_questions: dict[str, list[str]] = {}
        if user is not None:
            for saved in self._saved.list_saved_queries(user=user):
                if saved.question is None or not saved.metrics:
                    continue
                metric_domain = domain_for_metric(saved.metrics[0], self._resolver)
                if metric_domain is None:
                    continue
                extra_questions.setdefault(metric_domain, []).append(saved.question)
        return self._discovery.get_overview(
            domain, principal=principal, reports=report_refs, extra_questions=extra_questions
        )

    # ------------------------------------------------------------------
    # Query
    # ------------------------------------------------------------------

    def resolve_metric(
        self, name: str, context: str | None = None, *, principal: Principal | None = None
    ) -> Binding:
        """Resolve a metric name and return the :class:`Binding` (raises on unresolved/ambiguous).

        Raises the same ``Unresolved`` shape for a name outside ``principal``'s effective
        policy as for a nonexistent name (SPEC-E12 §3, S15 AC3).
        """
        return self._query.resolve_metric(name, context=context, principal=principal)

    def compile_query(
        self, query: SemanticQuery, *, principal: Principal | None = None
    ) -> CompileResult:
        """Compile a semantic query to SQL + metadata with no execution (SPEC §2).

        ``principal`` is bound by the adapter from a verified token/CLI override, never
        from ``query`` itself, and drives the compiler's tenant/role scoping (SPEC-E12).
        """
        return self._query.compile_query(query, principal=principal)

    async def query(
        self,
        query: SemanticQuery,
        *,
        harness: bool = False,
        caller: str | None = None,
        principal: Principal | None = None,
    ) -> QueryResult:
        """Compile and execute a semantic query read-only (SPEC §2)."""
        return await self._query.query(query, harness=harness, caller=caller, principal=principal)

    async def run_sql(
        self,
        sql: str,
        connection: str | None = None,
        *,
        caller: str | None = None,
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

    async def run_assertion(
        self, assertion: Assertion, *, resolver: ContractResolver | None = None
    ) -> AssertionOutcome:
        """Compile, execute read-only, and match one assertion (SPEC-Fuller-E15 §3.2)."""
        return await self._assertions.run_assertion(assertion, resolver=resolver)

    async def check_assertions(
        self, assertions: list[Assertion] | None = None
    ) -> list[AssertionOutcome]:
        """Run every executable assertion and return its outcome (SPEC-Fuller-E15 §3.4)."""
        return await self._assertions.check_assertions(assertions)

    async def run_accuracy_harness(
        self, assertions: list[Assertion] | None = None
    ) -> AccuracyReport:
        """Run the labeled assertion set and compute its accuracy (SPEC-Fuller-E15 §3.4)."""
        return await self._assertions.run_accuracy_harness(assertions)

    async def run_accuracy_baseline(
        self, assertions: list[Assertion] | None = None
    ) -> AccuracyReport:
        """Run the labeled assertion set against a schema-only resolver (SPEC-E16 Part 2 §2)."""
        return await self._assertions.run_accuracy_baseline(assertions)

    # ------------------------------------------------------------------
    # Knowledge (E6, P1)
    # ------------------------------------------------------------------

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

    def read_knowledge_page(
        self, page: str, *, user: str | None = None, principal: Principal | None = None
    ) -> dict[str, Any]:
        """Retrieve the full content of a knowledge page with live rendering (E6, P1)."""
        return self._knowledge.read_knowledge_page(page, user=user, principal=principal)

    # ------------------------------------------------------------------
    # Reports (AMENDMENT-curated-reports, P1)
    # ------------------------------------------------------------------

    def list_reports(
        self, domain: str | None = None, *, user: str | None = None
    ) -> list[ReportSummary]:
        """Directory listing of reports visible to ``user``: ``global/`` + their own scope.

        Never includes ``queries/`` content — use ``list_saved_queries`` for that (S20 AC2,
        S24 AC1).
        """
        return self._reports.list_reports(domain, user=user)

    async def run_report(
        self,
        report_id: str,
        *,
        as_of: datetime | None = None,
        filters: list[str] | None = None,
        user: str | None = None,
        caller: str | None = None,
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

    def describe_report(self, report_id: str, *, user: str | None = None) -> ReportStructure:
        """Return *report_id*'s section definitions (id, title, metrics, dimensions), unexecuted.

        The read-only counterpart to ``run_report``: how to discover a section's stable ``id``
        for ``update_report(remove_sections=...)`` without paying for query execution.
        """
        return self._reports.describe_report(report_id, user=user)

    def validate_reports(self) -> None:
        """Validate every committed report's sections compile and narrative refs resolve."""
        self._reports.validate_reports()

    # ------------------------------------------------------------------
    # Saved queries + personal reports (AMENDMENT-user-scoped-queries-reports, S19-S23, S25)
    # ------------------------------------------------------------------

    async def save_query(
        self,
        query: SemanticQuery,
        *,
        title: str | None = None,
        question: str | None = None,
        user: str | None = None,
        principal: Principal | None = None,
    ) -> SavedQuerySummary:
        """Validate ``query`` compiles, then save it under the caller's own ``queries/`` (S19).

        Raises :class:`~canonic.exc.TenantForbidden` when ``principal``'s role denies
        ``manage_saved_content``.
        """
        return await self._saved.save_query(
            query, title=title, question=question, user=user, principal=principal
        )

    def list_saved_queries(
        self, *, user: str | None = None, principal: Principal | None = None
    ) -> list[SavedQuerySummary]:
        """Directory listing of the caller's own saved queries only (S20)."""
        return self._saved.list_saved_queries(user=user, principal=principal)

    async def delete_query(
        self, query_id: str, *, user: str | None = None, principal: Principal | None = None
    ) -> None:
        """Remove one of the caller's own saved queries; refused outside their own scope (S21)."""
        await self._saved.delete_query(query_id, user=user, principal=principal)

    async def compose_report(
        self,
        title: str,
        from_: list[str],
        *,
        user: str | None = None,
        principal: Principal | None = None,
    ) -> ReportSummary:
        """Assemble N of the caller's own saved queries into a new personal report (S22)."""
        return await self._saved.compose_report(title, from_, user=user, principal=principal)

    async def update_report(
        self,
        report_id: str,
        *,
        add_from: list[str] | None = None,
        remove_sections: list[str] | None = None,
        user: str | None = None,
        principal: Principal | None = None,
    ) -> ReportSummary:
        """Append and/or remove sections on an existing personal report in place (S25)."""
        return await self._saved.update_report(
            report_id,
            add_from=add_from,
            remove_sections=remove_sections,
            user=user,
            principal=principal,
        )

    async def delete_report(
        self, report_id: str, *, user: str | None = None, principal: Principal | None = None
    ) -> None:
        """Remove one of the caller's own personal reports, never a query (S23)."""
        await self._saved.delete_report(report_id, user=user, principal=principal)
