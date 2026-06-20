"""Protocol-neutral capability layer — the single implementation of all core capabilities.

MCP and CLI adapters call this service; they do not duplicate any logic (SPEC §2.1).
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from pathlib import Path  # noqa: TC003 — used in function bodies, not just annotations
from typing import TYPE_CHECKING, Any, cast

from canon.compiler import SemanticQuery, compile
from canon.config import CanonConfig, load_config
from canon.connectors.base import Capability, require_capability
from canon.connectors.factory import connector_by_id
from canon.contract import CONTRACT_SCHEMA
from canon.contracts import ContractResolver
from canon.contracts.resolver import Ambiguous as ResolverAmbiguous
from canon.contracts.resolver import Binding
from canon.contracts.resolver import Unresolved as ResolverUnresolved
from canon.core.models import MetricDetail, MetricSummary, QueryResult, SourceFreshnessOut
from canon.exc import Ambiguous, CanonError, Unresolved
from canon.instrumentation.events import AnswerEventLog, DiskAnswerEventLog, NullAnswerEventLog
from canon.instrumentation.models import AnswerEvent, _age_days, _sha256_json
from canon.semantic.loader import list_semantic_sources

if TYPE_CHECKING:
    from canon.compiler.result import CompileResult
    from canon.connectors.base import ResultSet, SQLExecutable
    from canon.knowledge.results import SearchResult
    from canon.semantic.models import SemanticSource

__all__ = ["CanonService"]


class CanonService:
    """Capability layer loaded once per daemon/process (SPEC §2, §4).

    ``from_project`` is the normal entry point; tests can construct directly.
    """

    def __init__(
        self,
        config: CanonConfig,
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
        # name → source for fast lookup (sources have unique names within project)
        self._source_by_name: dict[str, SemanticSource] = {s.name: s for s in sources}

    @classmethod
    def from_project(cls, root: Path) -> CanonService:
        """Load config, resolver, and semantic sources from a project root."""
        config = load_config(root / "canon.yaml")
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
        summaries: list[MetricSummary] = []
        for binding in self._resolver._name_index.values():
            for b in binding:
                from canon.contracts.models import Status

                if b.status is not Status.ACTIVE:
                    continue
                summaries.append(
                    MetricSummary(
                        metric=b.metric,
                        source=b.canonical.source,
                        measure=b.canonical.measure,
                        status=b.status.value,
                        aliases=list(b.aliases),
                    )
                )
        # deduplicate by metric name (each metric may appear multiple times in the name index
        # via aliases) and sort for determinism
        seen: set[str] = set()
        deduped: list[MetricSummary] = []
        for s in sorted(summaries, key=lambda x: x.metric):
            if s.metric not in seen:
                seen.add(s.metric)
                deduped.append(s)
        return deduped

    def describe_metric(self, name: str) -> MetricDetail:
        """Return grain, dimensions, measures, and freshness for a metric (SPEC §4.1).

        ``dimensions`` includes every dimension queryable against this metric — both those
        declared on the owning source and those reachable via its declared join graph.  The
        compiler resolves dimensions globally across sources (SPEC §4 stage 2), so this list
        accurately reflects what can be passed as a dimension in a ``query()`` call.

        Raises :class:`canon.exc.Unresolved` or :class:`canon.exc.Ambiguous` when the
        name does not resolve to exactly one active binding.
        """
        binding = self._resolve_or_raise(name)
        source = self._source_by_name.get(binding.source)
        if source is None:
            raise Unresolved(
                f"metric {name!r} resolved to source {binding.source!r} but that source"
                " is not loaded — check semantics/"
            )
        freshness: SourceFreshnessOut | None = None
        if source.meta.last_validated_at is not None:
            freshness = SourceFreshnessOut(
                source=source.name,
                last_validated_at=source.meta.last_validated_at.isoformat(),
                stale=False,
            )
        return MetricDetail(
            metric=binding.metric,
            source=binding.source,
            measure=binding.measure,
            grain=list(source.grain),
            dimensions=self._reachable_dimensions(source.name),
            measures=[m.name for m in source.measures],
            aliases=list(binding.binding.aliases),
            freshness=freshness,
        )

    def _reachable_dimensions(self, source_name: str) -> list[str]:
        """All dimension names queryable from *source_name* via its declared join graph.

        Traverses the join tree breadth-first and collects dimensions from every reachable
        source.  Names are deduplicated (first occurrence wins) and the list is returned in
        BFS order so native dimensions always precede join-derived ones.
        """
        seen_sources: set[str] = set()
        queue: list[str] = [source_name]
        dims: list[str] = []
        seen_dims: set[str] = set()
        while queue:
            current_name = queue.pop(0)
            if current_name in seen_sources:
                continue
            seen_sources.add(current_name)
            current = self._source_by_name.get(current_name)
            if current is None:
                continue
            for d in current.dimensions:
                if d.name not in seen_dims:
                    dims.append(d.name)
                    seen_dims.add(d.name)
            for join in current.joins:
                if join.to not in seen_sources:
                    queue.append(join.to)
        return dims

    # ------------------------------------------------------------------
    # Core capabilities
    # ------------------------------------------------------------------

    def resolve_metric(self, name: str, context: str | None = None) -> Binding:
        """Resolve a metric name and return the :class:`Binding`.

        Raises :class:`canon.exc.Unresolved` (exit 2) or
        :class:`canon.exc.Ambiguous` (exit 3) on failure.
        """
        return self._resolve_or_raise(name, context=context)

    def compile_query(self, query: SemanticQuery) -> CompileResult:
        """Compile a semantic query to SQL + metadata with no execution (SPEC §2)."""
        return compile(query, self._resolver, self._sources)

    async def query(self, query: SemanticQuery) -> QueryResult:
        """Compile and execute a semantic query read-only (SPEC §2).

        Derives the connection from the primary metric's owning source.
        """
        started = time.perf_counter()
        compiled: CompileResult | None = None
        connection_id: str | None = None
        result: ResultSet | None = None
        error_code: str | None = None
        try:
            compiled = compile(query, self._resolver, self._sources)
            connection_id = self._connection_for_sql(compiled)
            connector = connector_by_id(self._config, connection_id)
            try:
                result = await cast(
                    "SQLExecutable", require_capability(connector, Capability.RUN_READ_ONLY_SQL)
                ).run_read_only_sql(compiled.sql)
            finally:
                await connector.aclose()
            return QueryResult.from_parts(compiled, result)
        except CanonError as err:
            error_code = err.code.value if err.code is not None else None
            raise
        finally:
            latency_ms = round((time.perf_counter() - started) * 1000)
            self._emit_answer_event(query, compiled, connection_id, result, latency_ms, error_code)

    def _emit_answer_event(
        self,
        query: SemanticQuery,
        compiled: CompileResult | None,
        connection_id: str | None,
        result: ResultSet | None,
        latency_ms: int,
        error_code: str | None,
    ) -> None:
        try:
            freshness: list[dict[str, Any]] = (
                [
                    {
                        "age_days": _age_days(f.last_validated_at),
                        "source": f.source,
                        "stale": f.stale,
                    }
                    for f in compiled.freshness
                ]
                if compiled is not None
                else []
            )
            event = AnswerEvent(
                ts=datetime.now(UTC).isoformat(),
                contract_schema=CONTRACT_SCHEMA,
                query_hash=_sha256_json(query.model_dump(mode="json")),
                compiled_sql_hash=_sha256_json({"sql": compiled.sql})
                if compiled is not None
                else None,
                connection=connection_id,
                resolved={"metrics": dict(compiled.resolved)} if compiled is not None else {},
                guardrails_fired=[g.id for g in compiled.guardrails_fired]
                if compiled is not None
                else [],
                freshness=freshness,
                latency_ms=latency_ms,
                bytes_scanned=result.bytes_scanned if result is not None else None,
                error=error_code,
            )
            self._event_log.append(event)
        except Exception:
            pass  # emission is a side effect — never raises into the serving path (SPEC-E16 §9)

    async def run_sql(self, sql: str, connection: str | None = None) -> ResultSet:
        """Execute a raw read-only SQL string on the named connection (SPEC §2).

        ``connection`` defaults to the project's ``default_connection``.
        Raises :class:`canon.exc.ReadOnlyViolation` (exit 11) for non-SELECT.
        """
        connector = connector_by_id(self._config, connection)
        try:
            return await cast(
                "SQLExecutable", require_capability(connector, Capability.RUN_READ_ONLY_SQL)
            ).run_read_only_sql(sql)
        finally:
            await connector.aclose()

    def search_knowledge(
        self,
        query: str,
        *,
        user: str | None = None,
        limit: int = 10,
    ) -> SearchResult:
        """Search knowledge pages for business context (E6, P1).

        Returns ranked hits (definitions, policies) and any caveats auto-surfaced
        because a hit references their bound semantic entity. Returns an empty
        result when no project root or knowledge directory is available.
        """
        from canon.knowledge import KnowledgeSearch, load_knowledge_page
        from canon.knowledge.results import SearchResult as SR

        if self._project_root is None:
            return SR(hits=[], caveats=[])
        knowledge_root = self._project_root / "knowledge"
        if not knowledge_root.exists():
            return SR(hits=[], caveats=[])

        pages = [load_knowledge_page(p) for p in sorted(knowledge_root.rglob("*.md"))]
        if not pages:
            return SR(hits=[], caveats=[])

        return KnowledgeSearch(pages).search(
            query, requesting_user=user or "anonymous", limit=limit
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _resolve_or_raise(self, name: str, context: str | None = None) -> Binding:
        result = self._resolver.resolve_metric(name, context=context)
        if isinstance(result, ResolverUnresolved):
            raise Unresolved(f"metric {name!r} matches no active binding")
        if isinstance(result, ResolverAmbiguous):
            raise Ambiguous(
                f"metric {name!r} is ambiguous",
                candidates=list(result.candidates),
            )
        return result

    def _connection_for_sql(self, compiled: CompileResult) -> str | None:
        """Pick the connection id from the first resolved metric's owning source."""
        for source_measure in compiled.resolved.values():
            source_name = source_measure.split(".")[0]
            source = self._source_by_name.get(source_name)
            if source is not None:
                return source.connection
        return None
