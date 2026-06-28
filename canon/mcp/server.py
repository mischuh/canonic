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
from canon.contract import CONTRACT_SCHEMA
from canon.core.models import CompileOutput
from canon.mcp.errors import canon_error_response

if TYPE_CHECKING:
    from canon.core.service import CanonService

__all__ = ["build_server"]

_INSTRUCTIONS = (
    "You are working with Canon, a semantic query layer over structured data.\n\n"
    "WORKFLOW — always follow these steps in order:\n"
    "1. Call list_metrics(). "
    "The response includes every metric AND its queryable dimensions with canonical names "
    "and human-readable labels. Use the 'name' field of each dimension in query() calls.\n"
    "2. Match the user's question to a metric ('metric' field) and a dimension ('name' in "
    "'dimensions'). Labels help map natural-language terms (e.g. 'Kundentyp' → "
    "label 'Customer Type' → name 'customer_type').\n"
    "3. Call query() with the canonical metric name and dimension name.\n\n"
    "Do NOT ask the user for clarification before calling list_metrics — the tool "
    "response provides everything you need."
)


def build_server(service: CanonService) -> FastMCP:
    """Return a :class:`FastMCP` instance with all P0 tools registered against *service*."""
    mcp: FastMCP = FastMCP("canon", instructions=_INSTRUCTIONS)

    # ------------------------------------------------------------------
    # Tool: list_metrics
    # ------------------------------------------------------------------

    @mcp.tool(
        description=(
            "Return the serving contract version this daemon implements. "
            "Call this at session start to confirm compatibility."
        )
    )
    async def contract_info() -> dict[str, str]:
        return {"contract_schema": CONTRACT_SCHEMA}

    # ------------------------------------------------------------------
    # Tool: negotiate_contract
    # ------------------------------------------------------------------

    @mcp.tool(
        description=(
            "Declare the contract_schema MAJOR your client was built against. "
            "The daemon accepts iff client MAJOR == server MAJOR; otherwise fails fast."
        )
    )
    async def negotiate_contract(contract_major: int) -> dict[str, Any]:
        server_major = int(CONTRACT_SCHEMA.split(".")[0])
        if contract_major != server_major:
            raise ValueError(
                f"contract_schema MAJOR mismatch: client declared {contract_major}, "
                f"server implements {CONTRACT_SCHEMA} (MAJOR {server_major}). "
                "Update your client or connect to a compatible Canon daemon."
            )
        return {"accepted": True, "contract_schema": CONTRACT_SCHEMA}

    # ------------------------------------------------------------------
    # Tool: list_metrics
    # ------------------------------------------------------------------

    @mcp.tool(
        description=(
            "List all active canonical metrics this project defines. "
            "Each metric includes its queryable dimensions with canonical names and labels — "
            "use these canonical 'name' values directly in query() calls."
        )
    )
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
            "filters (list[str]), via (list[str]), limit (int|null). "
            "Dimension names must be canonical 'name' values as returned by describe_metric — "
            "not natural-language terms. "
            "'via' routes join paths through specific intermediate sources — required when "
            "multiple join paths exist between the metric source and a dimension source. "
            "On an 'ambiguous_join_path' error, inspect the returned candidates: each has "
            "a 'via' list and a human-readable 'route'; re-issue with that 'via' value to "
            "select the desired path. "
            "On an 'unreachable' error for a dimension, check 'candidates' for the correct "
            "canonical name and re-issue."
        )
    )
    @canon_error_response
    async def compile_query(query: dict[str, Any]) -> dict[str, Any]:
        sq = SemanticQuery.model_validate(query)
        result = service.compile_query(sq)
        return CompileOutput.from_compile_result(result).model_dump(mode="json")

    # ------------------------------------------------------------------
    # Tool: query
    # ------------------------------------------------------------------

    @mcp.tool(
        description=(
            "Compile and execute a semantic query read-only. "
            "Returns rows + compiler metadata (resolved bindings, guardrails fired, freshness). "
            "Accepts a dict with keys: metrics (list[str]), dimensions (list[str]), "
            "filters (list[str]), via (list[str]), limit (int|null). "
            "Dimension names must be canonical 'name' values as returned by describe_metric — "
            "not natural-language terms. "
            "'via' routes join paths through specific intermediate sources — required when "
            "multiple join paths exist between the metric source and a dimension source. "
            "On an 'ambiguous_join_path' error, inspect the returned candidates: each has "
            "a 'via' list and a human-readable 'route'; re-issue with that 'via' value to "
            "select the desired path. "
            "On an 'unreachable' error for a dimension, check 'candidates' for the correct "
            "canonical name and re-issue."
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

    # ------------------------------------------------------------------
    # Tool: search_knowledge  (E6, P1)
    # ------------------------------------------------------------------

    @mcp.tool(
        description=(
            "Search the project's knowledge pages for business context: definitions, "
            "caveats, and policies. Returns ranked hits plus any caveats auto-surfaced "
            "because a hit references their bound semantic entity. "
            "Call alongside query() to get executable SQL and business meaning together. "
            "Returns empty hits when the project has no knowledge pages."
        )
    )
    @canon_error_response
    async def search_knowledge(
        query: str,
        user: str | None = None,
        limit: int = 5,
    ) -> dict[str, Any]:
        result = service.search_knowledge(query, user=user, limit=limit)
        return {
            "hits": [
                {
                    "page": h.page,
                    "summary": h.summary,
                    "usage_mode": h.usage_mode,
                    "matched_on": [m.value for m in h.matched_on],
                    "sl_refs": h.sl_refs,
                }
                for h in result.hits
            ],
            "caveats": [
                {
                    "page": c.page,
                    "summary": c.summary,
                    "triggered_by": c.triggered_by,
                }
                for c in result.caveats
            ],
        }

    # ------------------------------------------------------------------
    # Tool: read_knowledge_page  (E6, P1)
    # ------------------------------------------------------------------

    @mcp.tool(
        description=(
            "Retrieve the full content of a knowledge page by its id (page slug). "
            "Use after search_knowledge() to read the complete definition, caveat, or policy. "
            "Returns the rendered body (with live {{ sl:entity.expr }} definitions substituted), "
            "metadata, drift review flags, staleness warnings, and linked references."
        )
    )
    @canon_error_response
    async def read_knowledge_page(page: str, user: str | None = None) -> dict[str, Any]:
        return service.read_knowledge_page(page, user=user)

    return mcp
