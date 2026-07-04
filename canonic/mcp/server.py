"""FastMCP server — thin adapter over :class:`canonic.core.service.CanonicService` (SPEC E8 §4).

This module registers the six P0 MCP tools. Each tool does transport translation
only: parse arguments, call the service, serialise the result. No resolution,
compilation, or execution logic lives here (SPEC §2.1).

``build_server`` is the public factory; ``_mcp`` is the module-level instance used
by ``canonic mcp start`` (loaded after context is known) — callers must call
``build_server`` to inject the service before starting the server.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from fastmcp import FastMCP

from canonic.compiler.query import SemanticQuery
from canonic.contract import CONTRACT_SCHEMA
from canonic.core.models import CompileOutput
from canonic.mcp.errors import canonic_error_response

if TYPE_CHECKING:
    from canonic.core.service import CanonicService

__all__ = ["build_server"]

_INSTRUCTIONS = (
    "You are working with Canonic, a semantic query layer over structured data.\n\n"
    "WORKFLOW — always follow these steps in order:\n"
    "1. Call list_metrics(). "
    "The response includes every metric AND its queryable dimensions with canonical names "
    "and human-readable labels. Use the 'name' field of each dimension in query() calls.\n"
    "2. Match the user's question to a metric ('metric' field) and a dimension ('name' in "
    "'dimensions'). Labels help map natural-language terms (e.g. 'Kundentyp' → "
    "label 'Customer Type' → name 'customer_type').\n"
    "3. Call query() with the canonical metric name and dimension name.\n"
    "4. If the response contains a 'suggestions' key, relay that text verbatim to the user "
    "as a natural-language follow-up after presenting the results. "
    "Skip this step only when 'suggestions' is absent.\n\n"
    "Do NOT ask the user for clarification before calling list_metrics — the tool "
    "response provides everything you need.\n\n"
    "DEFINITIONAL / METHODOLOGY QUESTIONS — e.g. 'how is X calculated', 'what does X mean', "
    "'why is X computed this way':\n"
    "Always call search_knowledge() first, even if you already believe you know the answer "
    "from general knowledge. This project defines its own metrics and policies, which often "
    "diverge from textbook definitions. If a hit is returned, call read_knowledge_page() and "
    "base your answer strictly on that page's content — do not substitute a generic or "
    "invented formula. Relay any 'caveats' the same way you relay 'suggestions'. Only fall "
    "back to general knowledge if search_knowledge() returns no hits, and say so explicitly "
    "when you do."
)


def _format_suggestions(related: dict[str, Any]) -> str | None:
    """Format metadata.related into a verbatim-relay string for small models."""
    dims = related.get("unused_dimensions", [])
    metrics = related.get("sibling_metrics", [])
    if not dims and not metrics:
        return None
    parts: list[str] = []
    if dims:
        dim_tokens: list[str] = []
        for d in dims:
            label = d.get("label")
            name = d.get("name", "")
            dim_tokens.append(f"{label} ({name})" if label else name)
        parts.append("Break down further by: " + ", ".join(dim_tokens))
    if metrics:
        parts.append("Related metrics: " + ", ".join(m.get("name", "") for m in metrics))
    return " | ".join(parts)


def build_server(service: CanonicService, *, suggestions: bool = False) -> FastMCP:
    """Return a :class:`FastMCP` instance with all P0 tools registered against *service*."""
    mcp: FastMCP = FastMCP("canonic", instructions=_INSTRUCTIONS)

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
                "Update your client or connect to a compatible Canonic daemon."
            )
        return {"accepted": True, "contract_schema": CONTRACT_SCHEMA}

    # ------------------------------------------------------------------
    # Tool: get_overview  (E8 §4.1, P1)
    # ------------------------------------------------------------------

    @mcp.tool(
        description=(
            "Agent entry point: active metrics grouped by domain with plain-language sample "
            "questions. Call this first to understand what is askable. "
            "Pass 'domain' to narrow to one owning-source group."
        )
    )
    @canonic_error_response
    async def get_overview(domain: str | None = None) -> dict[str, Any]:
        overview = service.get_overview(domain=domain)
        return overview.model_dump(mode="json")

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
    @canonic_error_response
    async def list_metrics() -> list[dict[str, Any]]:
        summaries = service.list_metrics()
        return [s.model_dump() for s in summaries]

    # ------------------------------------------------------------------
    # Tool: describe_metric
    # ------------------------------------------------------------------

    @mcp.tool(description="Return grain, dimensions, measures, and freshness for one metric.")
    @canonic_error_response
    async def describe_metric(name: str) -> dict[str, Any]:
        detail = service.describe_metric(name)
        return detail.model_dump(mode="json")

    # ------------------------------------------------------------------
    # Tool: resolve_metric
    # ------------------------------------------------------------------

    @mcp.tool(
        description=(
            "Resolve a metric name or alias to its canonical binding. "
            "Returns the binding on success or a structured error on AMBIGUOUS/UNRESOLVED."
        )
    )
    @canonic_error_response
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
            "filters (list[str]): SQL WHERE predicates e.g. [\"segment = 'smb'\", \"status = 'active'\", \"created_at >= DATE('now', '-12 months')\"], "
            "via (list[str]), limit (int|null). "
            "Dimension names must be canonical 'name' values as returned by describe_metric — "
            "not natural-language terms. "
            "'via' routes join paths through specific intermediate sources — required when "
            "multiple join paths exist between the metric source and a dimension source. "
            "On an 'ambiguous_join_path' error, inspect the returned candidates: each has "
            "a 'via' list and a human-readable 'route'; re-issue with that 'via' value to "
            "select the desired path. "
            "On an 'unreachable' error for a dimension, check 'candidates' for the correct "
            "canonical name and re-issue. "
            "When the response contains a 'suggestions' key, relay that text verbatim to the "
            "user as a follow-up after presenting the results."
        )
    )
    @canonic_error_response
    async def compile_query(query: dict[str, Any]) -> dict[str, Any]:
        sq = SemanticQuery.model_validate(query)
        result = service.compile_query(sq)
        response = CompileOutput.from_compile_result(result).model_dump(mode="json")
        if suggestions:
            s = _format_suggestions(response.get("metadata", {}).get("related", {}))
            if s:
                response["suggestions"] = s
        return response

    # ------------------------------------------------------------------
    # Tool: query
    # ------------------------------------------------------------------

    @mcp.tool(
        description=(
            "Compile and execute a semantic query read-only. "
            "Returns rows + compiler metadata (resolved bindings, guardrails fired, freshness). "
            "Accepts a dict with keys: metrics (list[str]), dimensions (list[str]), "
            "filters (list[str]): SQL WHERE predicates e.g. [\"segment = 'smb'\", \"status = 'active'\", \"created_at >= DATE('now', '-12 months')\"], "
            "via (list[str]), limit (int|null). "
            "Dimension names must be canonical 'name' values as returned by describe_metric — "
            "not natural-language terms. "
            "'via' routes join paths through specific intermediate sources — required when "
            "multiple join paths exist between the metric source and a dimension source. "
            "On an 'ambiguous_join_path' error, inspect the returned candidates: each has "
            "a 'via' list and a human-readable 'route'; re-issue with that 'via' value to "
            "select the desired path. "
            "On an 'unreachable' error for a dimension, check 'candidates' for the correct "
            "canonical name and re-issue. "
            "When the response contains a 'suggestions' key, relay that text verbatim to the "
            "user as a follow-up after presenting the results."
        )
    )
    @canonic_error_response
    async def query(query: dict[str, Any]) -> dict[str, Any]:
        sq = SemanticQuery.model_validate(query)
        result = await service.query(sq)
        response = result.model_dump()
        if suggestions:
            s = _format_suggestions(response.get("metadata", {}).get("related", {}))
            if s:
                response["suggestions"] = s
        return response

    # ------------------------------------------------------------------
    # Tool: run_sql
    # ------------------------------------------------------------------

    @mcp.tool(
        description=(
            "Execute a read-only SQL SELECT on a named connection (or the project default). "
            "Rejects non-SELECT statements with READ_ONLY_VIOLATION."
        )
    )
    @canonic_error_response
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
            "Call this BEFORE answering any question about what a metric means or how it is "
            "calculated — this project's definitions may differ from general/textbook ones. "
            "Also call alongside query() to get executable SQL and business meaning together. "
            "Returns empty hits when the project has no knowledge pages."
        )
    )
    @canonic_error_response
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
    @canonic_error_response
    async def read_knowledge_page(page: str, user: str | None = None) -> dict[str, Any]:
        return service.read_knowledge_page(page, user=user)

    return mcp
