"""Protocol-neutral capability layer — the single implementation of all core capabilities.

MCP and CLI adapters call this service; they do not duplicate any logic (SPEC §2.1).
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from pathlib import Path  # noqa: TC003 — used in function bodies, not just annotations
from typing import TYPE_CHECKING, Any, cast

from canon.compiler import SemanticQuery, compile
from canon.compiler.joins import build_alias_tree
from canon.config import CanonConfig, load_config
from canon.connectors.base import Capability, require_capability
from canon.connectors.factory import default_factory
from canon.contract import CONTRACT_SCHEMA
from canon.contracts import ContractResolver
from canon.contracts.resolver import Ambiguous as ResolverAmbiguous
from canon.contracts.resolver import Binding
from canon.contracts.resolver import Unresolved as ResolverUnresolved
from canon.core.models import MetricDetail, MetricSummary, QueryResult, SourceFreshnessOut
from canon.exc import Ambiguous, CanonError, Unresolved, UnsupportedMeasure
from canon.instrumentation.events import AnswerEventLog, DiskAnswerEventLog, NullAnswerEventLog
from canon.instrumentation.models import AnswerEvent, _age_days, _sha256_json
from canon.semantic.loader import list_semantic_sources

if TYPE_CHECKING:
    from canon.compiler.result import CompileResult
    from canon.connectors.base import ResultSet, SQLExecutable
    from canon.contracts.assertions import AccuracyReport, AssertionOutcome
    from canon.contracts.models import Assertion
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
                from canon.contracts.models import BindingKind, Status

                if b.status is not Status.ACTIVE:
                    continue
                if b.canonical.kind not in {BindingKind.SINGLE, BindingKind.SEMI_ADDITIVE}:
                    continue  # ratio/weighted_avg have no single source+measure to summarize
                assert b.canonical.source is not None and b.canonical.measure is not None  # noqa: S101
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
        from canon.contracts.models import BindingKind

        if binding.kind not in {BindingKind.SINGLE, BindingKind.SEMI_ADDITIVE}:
            raise UnsupportedMeasure(
                f"metric {name!r} is a composite ({binding.kind}) — "
                "use query() to compute it; describe_metric() requires a single source+measure"
            )
        assert binding.source is not None and binding.measure is not None  # noqa: S101
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

        Traverses the join tree breadth-first using aliases. Dimensions reachable under
        only one alias are returned unqualified; dimensions reachable under multiple aliases
        (e.g. ``city`` via both ``pickup`` and ``dropoff``) are returned qualified
        (``pickup.city``, ``dropoff.city``) so the caller always gets usable names.
        """
        alias_to_source = build_alias_tree(source_name, self._source_by_name)

        # Collect (alias, dim_name) in BFS alias order.
        all_dims: list[tuple[str, str]] = []
        seen_aliases: set[str] = set()
        queue: list[str] = [source_name]
        while queue:
            alias = queue.pop(0)
            if alias in seen_aliases:
                continue
            seen_aliases.add(alias)
            src_name = alias_to_source.get(alias, alias)
            src = self._source_by_name.get(src_name)
            if src is None:
                continue
            for d in src.dimensions:
                all_dims.append((alias, d.name))
            for join in src.joins:
                child_alias = join.name or join.to
                if child_alias not in seen_aliases:
                    queue.append(child_alias)

        # Determine which dim_names appear under multiple aliases.
        dim_aliases: dict[str, list[str]] = {}
        for alias, dim_name in all_dims:
            dim_aliases.setdefault(dim_name, []).append(alias)

        seen_result: set[str] = set()
        result: list[str] = []
        for alias, dim_name in all_dims:
            entry = f"{alias}.{dim_name}" if len(dim_aliases[dim_name]) > 1 else dim_name
            if entry not in seen_result:
                seen_result.add(entry)
                result.append(entry)
        return result

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

    async def query(self, query: SemanticQuery, *, harness: bool = False) -> QueryResult:
        """Compile and execute a semantic query read-only (SPEC §2).

        Derives the connection from the primary metric's owning source.

        When ``harness`` is ``True`` (benchmark/CI mode, SPEC-Fuller-E15 §3.2 stage 9),
        every assertion matching this query is run after the user's query and a divergence
        raises :class:`~canon.exc.AssertionFailed` (exit 10). In normal mode the assertions
        are still evaluated for instrumentation (logged to the answer-event stream so E16 can
        spot stale assertions) but never block the result.
        """
        started = time.perf_counter()
        compiled: CompileResult | None = None
        connection_id: str | None = None
        result: ResultSet | None = None
        error_code: str | None = None
        try:
            compiled = compile(query, self._resolver, self._sources)
            connection_id = self._connection_for_sql(compiled)
            result = await self._execute(compiled.sql, connection_id)
            query_result = QueryResult.from_parts(compiled, result)
            await self._check_query_assertions(query, harness=harness)
            return query_result
        except CanonError as err:
            error_code = err.code.value if err.code is not None else None
            raise
        finally:
            latency_ms = round((time.perf_counter() - started) * 1000)
            self._emit_answer_event(query, compiled, connection_id, result, latency_ms, error_code)

    async def _execute(self, sql: str, connection_id: str | None) -> ResultSet:
        """Run read-only SQL on the resolved connection, always closing the connector."""
        connector = default_factory.for_id(self._config, connection_id)
        try:
            return await cast(
                "SQLExecutable", require_capability(connector, Capability.RUN_READ_ONLY_SQL)
            ).run_read_only_sql(sql)
        finally:
            await connector.aclose()

    # ------------------------------------------------------------------
    # Assertions (SPEC-Fuller-E15 §3) — the oracle for E16's accuracy harness
    # ------------------------------------------------------------------

    async def run_assertion(self, assertion: Assertion) -> AssertionOutcome:
        """Compile, execute read-only, and match one assertion (SPEC-Fuller-E15 §3.2).

        Compiles the assertion's *semantic* query (so it survives compiler changes),
        executes it read-only (E2), and compares the result to ``expect`` within tolerance.
        Returns a structured :class:`~canon.contracts.assertions.AssertionOutcome`; it never
        raises on a mismatch — callers (the CI gate, E16's harness) decide what a failure
        means. Raises :class:`~canon.exc.ValidationFailed` only when the assertion is not in
        executable semantic-query form.
        """
        from canon.contracts.assertions import assertion_to_query, match_result

        sq = assertion_to_query(assertion)
        compiled = compile(sq, self._resolver, self._sources)
        result = await self._execute(compiled.sql, self._connection_for_sql(compiled))
        return match_result(assertion, result, resolved=compiled.resolved)

    async def check_assertions(
        self, assertions: list[Assertion] | None = None
    ) -> list[AssertionOutcome]:
        """Run every executable assertion and return its outcome (SPEC-Fuller-E15 §3.4).

        Defaults to all loaded assertions (E16's accuracy harness passes the full set);
        non-executable candidate assertions are skipped. Outcomes are returned in input
        order so ``accuracy = passed / total`` is deterministic.
        """
        from canon.contracts.assertions import is_executable

        candidates = assertions if assertions is not None else self._resolver.all_assertions()
        outcomes: list[AssertionOutcome] = []
        for assertion in candidates:
            if not is_executable(assertion):
                continue
            outcomes.append(await self.run_assertion(assertion))
        return outcomes

    async def run_accuracy_harness(
        self, assertions: list[Assertion] | None = None
    ) -> AccuracyReport:
        """Run the labeled assertion set and compute its accuracy (SPEC-Fuller-E15 §3.4).

        This is the E16 integration: every executable assertion is the oracle for one labeled
        question, so the returned :class:`~canon.contracts.assertions.AccuracyReport` carries
        ``accuracy = passed / total``. Outcomes preserve load order, so the same assertion set
        yields the same number every run — the property that makes a regression detectable. The
        report never raises on a mismatch; the CI gate decides what a sub-target number means.
        """
        from canon.contracts.assertions import accuracy_report

        return accuracy_report(await self.check_assertions(assertions))

    async def _check_query_assertions(self, query: SemanticQuery, *, harness: bool) -> None:
        """Evaluate assertions matching a user query (SPEC-Fuller-E15 §3.2).

        Under ``harness`` the first failing assertion raises :class:`~canon.exc.AssertionFailed`
        (the CI gate). In normal mode the assertions are evaluated for instrumentation only —
        any mismatch or evaluation error is swallowed so a stale assertion never blocks a
        user's query (AC2). E16's accuracy harness (#110) owns durable assertion-outcome
        persistence.
        """
        matching = self._resolver.assertions_for(query.model_dump(mode="json"))
        if not matching:
            return
        if not harness:
            import contextlib

            with contextlib.suppress(Exception):
                # Informational only — a stale assertion must never block the query (AC2).
                await self.check_assertions(matching)
            return
        from canon.exc import AssertionFailed

        for outcome in await self.check_assertions(matching):
            if not outcome.passed:
                raise AssertionFailed(outcome.detail, assertion_id=outcome.assertion_id)

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
        connector = default_factory.for_id(self._config, connection)
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
        from canon.knowledge import EntityIndex, KnowledgeSearch, load_knowledge_page
        from canon.knowledge.results import SearchResult as SR

        if self._project_root is None:
            return SR(hits=[], caveats=[])
        knowledge_root = self._project_root / "knowledge"
        if not knowledge_root.exists():
            return SR(hits=[], caveats=[])

        pages = [load_knowledge_page(p) for p in sorted(knowledge_root.rglob("*.md"))]
        if not pages:
            return SR(hits=[], caveats=[])

        # Live entity index so a returned page whose bound measure definition drifted is
        # flagged for prose review (§7).
        entity_index = EntityIndex.from_sources(self._sources)
        return KnowledgeSearch(pages, entity_index=entity_index).search(
            query, requesting_user=user or "anonymous", limit=limit
        )

    def read_knowledge_page(self, page: str, *, user: str | None = None) -> dict[str, Any]:
        """Retrieve the full content of a knowledge page by page id with live rendering (E6, P1).

        Returns rendered body (with {{ sl:entity.expr }} directives resolved to live SQL),
        drift flag, and staleness metadata. Respects access control.
        Per amendment-knowledge-read-page: body is rendered, meta includes last_validated_at and drift_flag.
        """
        from canon.knowledge import load_knowledge_page, user_from_path
        from canon.knowledge.drift import DriftDetector
        from canon.knowledge.rendering import DefinitionRenderer
        from canon.knowledge.validation import EntityIndex

        if self._project_root is None:
            raise KeyError(f"No project root; cannot load knowledge page {page!r}")
        knowledge_root = self._project_root / "knowledge"
        if not knowledge_root.exists():
            raise KeyError(f"Knowledge directory not found; cannot load page {page!r}")

        pages = [load_knowledge_page(p) for p in sorted(knowledge_root.rglob("*.md"))]
        requesting_user = user or "anonymous"

        knowledge_page = None
        for p in pages:
            if p.id == page:
                page_owner = user_from_path(p.path)
                if p.scope.value == "global" or page_owner == requesting_user:
                    knowledge_page = p
                    break
                raise PermissionError(
                    f"User {requesting_user!r} does not have access to page {page!r}"
                )

        if knowledge_page is None:
            raise KeyError(f"Knowledge page {page!r} not found")

        # Live entity index for rendering and drift detection (E6 §7).
        entity_index = EntityIndex.from_sources(self._sources)

        # Render body with live measure definitions ({{ sl:entity.expr }} → live SQL).
        renderer = DefinitionRenderer(entity_index)
        rendered_body = renderer.render(knowledge_page)

        # Detect drift: compare recorded bound_fingerprints with live definitions.
        detector = DriftDetector()
        drifted_refs = detector.flagged_for_review(knowledge_page, entity_index)
        has_drift = len(drifted_refs) > 0

        return {
            "page_id": knowledge_page.id,
            "scope": knowledge_page.scope.value,
            "summary": knowledge_page.summary,
            "body": rendered_body,
            "tags": knowledge_page.tags,
            "sl_refs": knowledge_page.sl_refs,
            "refs": knowledge_page.refs,
            "usage_mode": knowledge_page.usage_mode.value,
            "meta": {
                "last_validated_at": (
                    knowledge_page.meta.last_validated_at.isoformat()
                    if knowledge_page.meta.last_validated_at
                    else None
                ),
                "drift_flag": has_drift,
            },
        }

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
