"""Query capabilities: compile, execute read-only, emit answer events (SPEC §2)."""

from __future__ import annotations

import logging
import time
import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, cast

from canonic.compiler import compile
from canonic.connectors.base import Capability, require_capability
from canonic.contract import CONTRACT_SCHEMA
from canonic.core.models import QueryResult
from canonic.exc import CanonicError
from canonic.feedback.history import BindingOutcomeHistory
from canonic.instrumentation.models import AnswerEvent, _age_days, _sha256_json
from canonic.log import query_id_var
from canonic.trust.scorer import trust_for_compiled

if TYPE_CHECKING:
    from canonic.compiler import SemanticQuery
    from canonic.compiler.result import CompileResult
    from canonic.connectors.base import ResultSet, SQLExecutable
    from canonic.contracts.resolver import Binding
    from canonic.core.assertions import AssertionService
    from canonic.core.context import ServiceContext

logger = logging.getLogger(__name__)


class QueryService:
    """Compile and execute semantic queries and raw SQL, emitting answer events."""

    def __init__(self, ctx: ServiceContext, assertions: AssertionService) -> None:
        self._ctx = ctx
        self._assertions = assertions

    def resolve_metric(self, name: str, context: str | None = None) -> Binding:
        """Resolve a metric name and return the :class:`Binding`.

        Raises :class:`canonic.exc.Unresolved` (exit 2) or
        :class:`canonic.exc.Ambiguous` (exit 3) on failure.
        """
        return self._ctx.resolve_or_raise(name, context=context)

    def compile_query(self, query: SemanticQuery) -> CompileResult:
        """Compile a semantic query to SQL + metadata with no execution (SPEC §2)."""
        return compile(
            query,
            self._ctx.resolver,
            self._ctx.sources,
            connection_dialects=self._ctx.connection_dialects,
        )

    async def query(
        self, query: SemanticQuery, *, harness: bool = False, caller: str | None = None
    ) -> QueryResult:
        """Compile and execute a semantic query read-only (SPEC §2).

        Derives the connection from the primary metric's owning source.

        When ``harness`` is ``True`` (benchmark/CI mode, SPEC-Fuller-E15 §3.2 stage 9),
        every assertion matching this query is run after the user's query and a divergence
        raises :class:`~canonic.exc.AssertionFailed` (exit 10). In normal mode the assertions
        are still evaluated for instrumentation (logged to the answer-event stream so E16 can
        spot stale assertions) but never block the result.

        ``caller`` is the verified bearer-token client_id for MCP http-transport calls
        (``None`` for stdio/CLI, which have no auth layer); recorded on the emitted
        answer event for per-user attribution (AMENDMENT-remote-mcp-transport.md).
        """
        started = time.perf_counter()
        compiled: CompileResult | None = None
        connection_id: str | None = None
        result: ResultSet | None = None
        error_code: str | None = None
        outcome_history: BindingOutcomeHistory | None = None
        qid_token = query_id_var.set(uuid.uuid4().hex[:8])
        logger.info(
            "query received: metrics=%s dimensions=%d",
            query.metrics,
            len(query.dimensions),
        )
        try:
            compiled = compile(
                query,
                self._ctx.resolver,
                self._ctx.sources,
                connection_dialects=self._ctx.connection_dialects,
            )
            connection_id = self._ctx.connection_for_sql(compiled)
            if connection_id is not None:
                logger.info("connection selected: id=%s", connection_id)
            else:
                logger.info(
                    "connection selected: id=%s (project default; no source-level match)",
                    self._ctx.config.project.default_connection,
                )
            result = await self._ctx.execute(compiled.sql, connection_id)
            outcome_history = self._load_outcome_history()
            query_result = QueryResult.from_parts(
                compiled,
                result,
                outcome_history=outcome_history,
                outcome_window_days=self._ctx.config.feedback.trust_cap_window_days,
            )
            await self._assertions.check_query_assertions(query, harness=harness)
            return query_result
        except CanonicError as err:
            error_code = err.code.value if err.code is not None else None
            raise
        finally:
            latency_ms = round((time.perf_counter() - started) * 1000)
            if result is not None:
                logger.info("query completed: latency_ms=%d rows=%d", latency_ms, len(result.rows))
            self._emit_answer_event(
                query,
                compiled,
                connection_id,
                result,
                latency_ms,
                error_code,
                outcome_history,
                caller=caller,
            )
            query_id_var.reset(qid_token)

    def _load_outcome_history(self) -> BindingOutcomeHistory | None:
        """Load the current per-binding outcome history, or None with no project root.

        Read fresh on every call (matching :func:`canonic.instrumentation.report.read_events`'s
        own convention) rather than cached on the service instance, so a long-lived daemon
        reflects an outcome marked between queries without a restart (SPEC-E11 §5).
        """
        if self._ctx.project_root is None:
            return None
        return BindingOutcomeHistory.from_project(self._ctx.project_root)

    async def run_sql(
        self, sql: str, connection: str | None = None, *, caller: str | None = None
    ) -> ResultSet:
        """Execute a raw read-only SQL string on the named connection (SPEC §2).

        ``connection`` defaults to the project's ``default_connection``.
        Raises :class:`canonic.exc.ReadOnlyViolation` (exit 11) for non-SELECT.

        ``caller`` is the verified bearer-token client_id for MCP http-transport calls
        (``None`` for stdio/CLI); recorded on the emitted answer event for per-user
        attribution (AMENDMENT-remote-mcp-transport.md).
        """
        started = time.perf_counter()
        result: ResultSet | None = None
        error_code: str | None = None
        connector = self._ctx.connector_for(connection)
        try:
            result = await cast(
                "SQLExecutable", require_capability(connector, Capability.RUN_READ_ONLY_SQL)
            ).run_read_only_sql(sql)
            return result
        except CanonicError as err:
            error_code = err.code.value if err.code is not None else None
            raise
        finally:
            await connector.aclose()
            latency_ms = round((time.perf_counter() - started) * 1000)
            self._emit_sql_event(sql, connection, result, latency_ms, error_code, caller)

    def _emit_answer_event(
        self,
        query: SemanticQuery,
        compiled: CompileResult | None,
        connection_id: str | None,
        result: ResultSet | None,
        latency_ms: int,
        error_code: str | None,
        outcome_history: BindingOutcomeHistory | None = None,
        *,
        caller: str | None = None,
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
                trust_score=trust_for_compiled(
                    compiled,
                    result,
                    outcome_history=outcome_history,
                    outcome_window_days=self._ctx.config.feedback.trust_cap_window_days,
                ).tier.value
                if compiled is not None
                else None,
                user=caller,
            )
            self._ctx.event_log.append(event)
        except Exception as exc:
            logger.warning("answer event emission failed: %s", exc)

    def _emit_sql_event(
        self,
        sql: str,
        connection_id: str | None,
        result: ResultSet | None,
        latency_ms: int,
        error_code: str | None,
        caller: str | None,
    ) -> None:
        """Answer-event counterpart of :meth:`_emit_answer_event` for the raw-SQL escape hatch.

        ``run_sql`` has no :class:`SemanticQuery`/compiled query to hash or derive
        resolved bindings/trust score from, so this builds a minimal :class:`AnswerEvent`
        directly rather than overloading ``_emit_answer_event``'s signature.
        """
        try:
            event = AnswerEvent(
                ts=datetime.now(UTC).isoformat(),
                contract_schema=CONTRACT_SCHEMA,
                query_hash=_sha256_json({"sql": sql}),
                compiled_sql_hash=None,
                connection=connection_id,
                latency_ms=latency_ms,
                bytes_scanned=result.bytes_scanned if result is not None else None,
                error=error_code,
                user=caller,
            )
            self._ctx.event_log.append(event)
        except Exception as exc:
            logger.warning("answer event emission failed: %s", exc)
