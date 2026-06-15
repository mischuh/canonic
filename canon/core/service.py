"""Protocol-neutral capability layer — the single implementation of all core capabilities.

MCP and CLI adapters call this service; they do not duplicate any logic (SPEC §2.1).
"""

from __future__ import annotations

from pathlib import Path  # noqa: TC003 — used in function bodies, not just annotations
from typing import TYPE_CHECKING

from canon.compiler import SemanticQuery, compile
from canon.config import CanonConfig, load_config
from canon.connectors.factory import connector_by_id
from canon.contracts import ContractResolver
from canon.contracts.resolver import Ambiguous as ResolverAmbiguous
from canon.contracts.resolver import Binding
from canon.contracts.resolver import Unresolved as ResolverUnresolved
from canon.core.models import MetricDetail, MetricSummary, QueryResult, SourceFreshnessOut
from canon.exc import Ambiguous, Unresolved
from canon.semantic.loader import list_semantic_sources

if TYPE_CHECKING:
    from canon.compiler.result import CompileResult
    from canon.connectors.base import ResultSet
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
    ) -> None:
        self._config = config
        self._resolver = resolver
        self._sources = sources
        # name → source for fast lookup (sources have unique names within project)
        self._source_by_name: dict[str, SemanticSource] = {s.name: s for s in sources}

    @classmethod
    def from_project(cls, root: Path) -> CanonService:
        """Load config, resolver, and semantic sources from a project root."""
        config = load_config(root / "canon.yaml")
        resolver = ContractResolver.from_project(root)
        sources = list_semantic_sources(root)
        return cls(config=config, resolver=resolver, sources=sources)

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
            dimensions=[d.name for d in source.dimensions],
            measures=[m.name for m in source.measures],
            aliases=list(binding.binding.aliases),
            freshness=freshness,
        )

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
        compiled = compile(query, self._resolver, self._sources)
        connection_id = self._connection_for_sql(compiled)
        connector = connector_by_id(self._config, connection_id)
        try:
            result: ResultSet = await connector.run_read_only_sql(compiled.sql)
        finally:
            await connector.aclose()
        return QueryResult.from_parts(compiled, result)

    async def run_sql(self, sql: str, connection: str | None = None) -> ResultSet:
        """Execute a raw read-only SQL string on the named connection (SPEC §2).

        ``connection`` defaults to the project's ``default_connection``.
        Raises :class:`canon.exc.ReadOnlyViolation` (exit 11) for non-SELECT.
        """
        connector = connector_by_id(self._config, connection)
        try:
            return await connector.run_read_only_sql(sql)
        finally:
            await connector.aclose()

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
