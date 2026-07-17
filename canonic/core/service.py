"""Protocol-neutral capability layer — the single implementation of all core capabilities.

MCP and CLI adapters call this service; they do not duplicate any logic (SPEC §2.1).

:class:`CanonicService` is a thin facade: it wires the injected dependencies into a shared
:class:`~canonic.core.context.ServiceContext` and delegates each capability to one of four
focused collaborators — :class:`~canonic.core.discovery.DiscoveryService`,
:class:`~canonic.core.query.QueryService`, :class:`~canonic.core.assertions.AssertionService`,
and :class:`~canonic.core.knowledge.KnowledgeService`. The public method surface is unchanged,
so the CLI/MCP adapters and their byte-identical parity are unaffected.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from canonic.compiler.dialect import adapter_for
from canonic.config import CanonicConfig, Connection, load_config
from canonic.contracts import ContractResolver
from canonic.core.assertions import AssertionService
from canonic.core.context import ServiceContext
from canonic.core.discovery import DiscoveryService
from canonic.core.knowledge import KnowledgeService
from canonic.core.query import QueryService
from canonic.instrumentation.events import AnswerEventLog, DiskAnswerEventLog, NullAnswerEventLog
from canonic.semantic.loader import list_semantic_sources

if TYPE_CHECKING:
    from typing import Any

    from canonic.compiler import SemanticQuery
    from canonic.compiler.result import CompileResult
    from canonic.connectors.base import ResultSet
    from canonic.contracts.assertions import AccuracyReport, AssertionOutcome
    from canonic.contracts.models import Assertion
    from canonic.contracts.resolver import Binding
    from canonic.core.models import MetricDetail, MetricSummary, OverviewResult, QueryResult
    from canonic.knowledge.results import SearchResult
    from canonic.semantic.models import SemanticSource
    from canonic.trust.models import TrustScore

__all__ = ["CanonicService"]


def _dialect_for_type(connector_type: str) -> str:
    """Map a connector type string to a sqlglot dialect name.

    Most connector types are already valid sqlglot dialect names (duckdb, sqlite, postgres).
    This function normalises the few that are not and validates unknown types via adapter_for.
    """
    _OVERRIDES: dict[str, str] = {
        "postgresql": "postgres",
        "pg": "postgres",
    }
    dialect = _OVERRIDES.get(connector_type, connector_type)
    # Validate via adapter_for so unknown types silently fall back to postgres.
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
        # connection id → sqlglot dialect name, derived from connection types in config
        connection_dialects: dict[str, str] = {
            c.id: _dialect_for_type(c.type) for c in config.connections
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

    def list_metrics(self) -> list[MetricSummary]:
        """Return a summary of every active canonical metric (SPEC §4.1)."""
        return self._discovery.list_metrics()

    def trust_report(self) -> list[tuple[str, TrustScore]]:
        """Static trust tier for every active canonical metric, sorted by name (SPEC-E14 §8)."""
        return self._discovery.trust_report()

    def describe_metric(self, name: str) -> MetricDetail:
        """Return grain, dimensions, measures, and freshness for a metric (SPEC §4.1)."""
        return self._discovery.describe_metric(name)

    def get_overview(self, domain: str | None = None) -> OverviewResult:
        """Return active metrics grouped by domain with sample questions (S12)."""
        return self._discovery.get_overview(domain)

    # ------------------------------------------------------------------
    # Query
    # ------------------------------------------------------------------

    def resolve_metric(self, name: str, context: str | None = None) -> Binding:
        """Resolve a metric name and return the :class:`Binding` (raises on unresolved/ambiguous)."""
        return self._query.resolve_metric(name, context=context)

    def compile_query(self, query: SemanticQuery) -> CompileResult:
        """Compile a semantic query to SQL + metadata with no execution (SPEC §2)."""
        return self._query.compile_query(query)

    async def query(
        self, query: SemanticQuery, *, harness: bool = False, caller: str | None = None
    ) -> QueryResult:
        """Compile and execute a semantic query read-only (SPEC §2)."""
        return await self._query.query(query, harness=harness, caller=caller)

    async def run_sql(
        self, sql: str, connection: str | None = None, *, caller: str | None = None
    ) -> ResultSet:
        """Execute a raw read-only SQL string on the named connection (SPEC §2)."""
        return await self._query.run_sql(sql, connection, caller=caller)

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
    ) -> SearchResult:
        """Search knowledge pages for business context (E6, P1)."""
        return self._knowledge.search_knowledge(query, user=user, limit=limit)

    def read_knowledge_page(self, page: str, *, user: str | None = None) -> dict[str, Any]:
        """Retrieve the full content of a knowledge page with live rendering (E6, P1)."""
        return self._knowledge.read_knowledge_page(page, user=user)
