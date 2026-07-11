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
from fastmcp.server.dependencies import get_access_token

from canonic.compiler.query import SemanticQuery
from canonic.contract import CONTRACT_SCHEMA
from canonic.core.models import CompileOutput
from canonic.mcp.errors import canonic_error_response

if TYPE_CHECKING:
    from fastmcp.server.auth.auth import TokenVerifier

    from canonic.core.service import CanonicService


def _caller_id() -> str | None:
    """The verified client_id for the current request, or ``None`` under stdio.

    ``stdio`` transport has no ``AccessToken`` (no auth layer); ``http`` transport
    always has one once a request is authenticated (unauthenticated requests never
    reach a tool body — FastMCP's auth middleware rejects them with 401 first).
    """
    token = get_access_token()
    return token.client_id if token is not None else None


__all__ = ["build_server"]

_INSTRUCTIONS = (
    "You are working with Canonic, a semantic query layer over structured data.\n\n"
    "WORKFLOW — always follow these steps in order:\n"
    "1. Call list_metrics(). "
    "The response has two top-level keys: 'metrics' (each with a 'dimensions' list of "
    "canonical names queryable against it) and 'dimensions' (a deduplicated catalog mapping "
    "each canonical name to its 'label' and 'source' — look names up there for the "
    "human-readable label). Use the canonical name in query() calls.\n"
    "2. Match the user's question to a metric ('metric' field) and a dimension name from its "
    "'dimensions' list. Look that name up in the top-level 'dimensions' catalog for its label "
    "to map natural-language terms (e.g. 'Kundentyp' → label 'Customer Type' → "
    "name 'customer_type').\n"
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
    "when you do.\n\n"
    "RAW SQL (run_sql) — only use this when no metric/dimension in list_metrics() covers the "
    "question. Prefer query()/compile_query() whenever a metric exists: they route joins "
    "through the resolved join graph and apply guardrails (e.g. against fan-out from "
    "one-to-many joins), which a hand-written JOIN across fact tables will not get and can "
    "silently multiply values such as revenue.\n\n"
    "JOIN AMBIGUITY — on an 'ambiguous_join_path' error from query()/compile_query(), never "
    "pick a candidate yourself and retry silently. The candidate routes encode different "
    "business meaning (e.g. 'via the specific rental' vs 'via the vehicle generically') that "
    "only the user can resolve, not a naming mistake you can correct on your own. Describe "
    "the candidate routes to the user in plain language and ask which one matches their "
    "intent; only re-issue the query with the chosen 'via' after they answer. This is "
    "different from an 'unreachable' error, where re-issuing with the corrected canonical "
    "name from 'candidates' is fine without asking, since that is a naming fix, not a "
    "business decision.\n\n"
    "NEVER return a result you have identified as suspicious — inconsistent with a prior "
    "answer, an implausible magnitude, or produced by a join/aggregation you are unsure is "
    "correct. Do not present it as the final answer. Instead say explicitly that the number "
    "looks wrong and why, then re-derive it via query() or ask the user before answering."
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


def build_server(
    service: CanonicService, *, suggestions: bool = False, auth: TokenVerifier | None = None
) -> FastMCP:
    """Return a :class:`FastMCP` instance with all P0 tools registered against *service*.

    ``auth`` is ``None`` for ``stdio`` transport (no auth layer). ``http`` transport
    always passes a resolved :class:`~canonic.mcp.auth.CanonicTokenVerifier` — see
    ``canonic.mcp.daemon.start_http``.
    """
    mcp: FastMCP = FastMCP("canonic", instructions=_INSTRUCTIONS, auth=auth)

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
            "List all active canonical metrics this project defines, plus a deduplicated "
            "catalog of every dimension queryable against them. Each metric's 'dimensions' "
            "list holds canonical names only — look those names up in the top-level "
            "'dimensions' catalog for the label/source. Use canonical names directly in "
            "query() calls."
        )
    )
    @canonic_error_response
    async def list_metrics() -> dict[str, Any]:
        summaries = service.list_metrics()
        dim_catalog: dict[str, dict[str, Any]] = {}
        metrics_out: list[dict[str, Any]] = []
        for s in summaries:
            metric = s.model_dump(exclude={"dimensions"})
            metric["dimensions"] = [d.name for d in s.dimensions]
            metrics_out.append(metric)
            for d in s.dimensions:
                dim_catalog.setdefault(d.name, d.model_dump())
        return {
            "metrics": metrics_out,
            "dimensions": [dim_catalog[name] for name in sorted(dim_catalog)],
        }

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
            "On an 'ambiguous_join_path' error, do NOT pick a candidate and re-issue on your "
            "own — the candidate routes encode different business meaning that only the user "
            "can decide. Each candidate has a 'via' list and a human-readable 'route'; "
            "describe the route options to the user in plain language and ask which one "
            "matches their intent, then re-issue with the chosen 'via' after they answer. "
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
            "On an 'ambiguous_join_path' error, do NOT pick a candidate and re-issue on your "
            "own — the candidate routes encode different business meaning that only the user "
            "can decide. Each candidate has a 'via' list and a human-readable 'route'; "
            "describe the route options to the user in plain language and ask which one "
            "matches their intent, then re-issue with the chosen 'via' after they answer. "
            "On an 'unreachable' error for a dimension, check 'candidates' for the correct "
            "canonical name and re-issue. "
            "When the response contains a 'suggestions' key, relay that text verbatim to the "
            "user as a follow-up after presenting the results."
        )
    )
    @canonic_error_response
    async def query(query: dict[str, Any]) -> dict[str, Any]:
        sq = SemanticQuery.model_validate(query)
        result = await service.query(sq, caller=_caller_id())
        response = result.model_dump(mode="json")
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
        result = await service.run_sql(sql, connection=connection, caller=_caller_id())
        return result.model_dump(mode="json")

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
