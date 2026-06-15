"""FastMCP server — thin adapter over :class:`canon.core.service.CanonService` (SPEC E8 §4).

This module registers the six P0 MCP tools. Each tool does transport translation
only: parse arguments, call the service, serialise the result. No resolution,
compilation, or execution logic lives here (SPEC §2.1).

``build_server`` is the public factory; ``_mcp`` is the module-level instance used
by ``canon mcp start`` (loaded after context is known) — callers must call
``build_server`` to inject the service before starting the server.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from fastmcp import FastMCP

from canon.compiler.query import SemanticQuery
from canon.mcp.errors import canon_error_response

if TYPE_CHECKING:
    from canon.core.service import CanonService

__all__ = ["build_server"]


def build_server(service: CanonService) -> FastMCP:
    """Return a :class:`FastMCP` instance with all P0 tools registered against *service*."""
    mcp: FastMCP = FastMCP("canon")

    # ------------------------------------------------------------------
    # Tool: list_metrics
    # ------------------------------------------------------------------

    @mcp.tool(description="List all active canonical metrics this project defines.")
    @canon_error_response
    async def list_metrics() -> list[dict[str, Any]]:
        summaries = service.list_metrics()
        return [s.model_dump() for s in summaries]

    # ------------------------------------------------------------------
    # Tool: describe_metric
    # ------------------------------------------------------------------

    @mcp.tool(description="Return grain, dimensions, measures, and freshness for one metric.")
    @canon_error_response
    async def describe_metric(name: str) -> dict[str, Any]:
        detail = service.describe_metric(name)
        return detail.model_dump()

    # ------------------------------------------------------------------
    # Tool: resolve_metric
    # ------------------------------------------------------------------

    @mcp.tool(
        description=(
            "Resolve a metric name or alias to its canonical binding. "
            "Returns the binding on success or a structured error on AMBIGUOUS/UNRESOLVED."
        )
    )
    @canon_error_response
    async def resolve_metric(name: str, context: str | None = None) -> dict[str, Any]:
        binding = service.resolve_metric(name, context=context)
        return {
            "metric": binding.metric,
            "source": binding.source,
            "measure": binding.measure,
        }

    # ------------------------------------------------------------------
    # Tool: compile_query
    # ------------------------------------------------------------------

    @mcp.tool(
        description=(
            "Compile a semantic query to dialect-correct SQL + metadata without executing it. "
            "Accepts a dict with keys: metrics (list[str]), dimensions (list[str]), "
            "filters (list[str]), limit (int|null)."
        )
    )
    @canon_error_response
    async def compile_query(query: dict[str, Any]) -> dict[str, Any]:
        sq = SemanticQuery.model_validate(query)
        result = service.compile_query(sq)
        return {
            "sql": result.sql,
            "dialect": result.dialect,
            "resolved": result.resolved,
            "guardrails_fired": [{"id": g.id, "kind": g.kind} for g in result.guardrails_fired],
            "freshness": [
                {
                    "source": f.source,
                    "last_validated_at": f.last_validated_at,
                    "stale": f.stale,
                }
                for f in result.freshness
            ],
        }

    # ------------------------------------------------------------------
    # Tool: query
    # ------------------------------------------------------------------

    @mcp.tool(
        description=(
            "Compile and execute a semantic query read-only. "
            "Returns rows + compiler metadata (resolved bindings, guardrails fired, freshness). "
            "Accepts a dict with keys: metrics (list[str]), dimensions (list[str]), "
            "filters (list[str]), limit (int|null)."
        )
    )
    @canon_error_response
    async def query(query: dict[str, Any]) -> dict[str, Any]:
        sq = SemanticQuery.model_validate(query)
        result = await service.query(sq)
        return result.model_dump()

    # ------------------------------------------------------------------
    # Tool: run_sql
    # ------------------------------------------------------------------

    @mcp.tool(
        description=(
            "Execute a read-only SQL SELECT on a named connection (or the project default). "
            "Rejects non-SELECT statements with READ_ONLY_VIOLATION."
        )
    )
    @canon_error_response
    async def run_sql(sql: str, connection: str | None = None) -> dict[str, Any]:
        result = await service.run_sql(sql, connection=connection)
        return result.model_dump()

    return mcp
