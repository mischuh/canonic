"""Walking-skeleton e2e + parity conformance (GH-14).

One query, two surfaces, identical result — against a live Postgres. Proves the
adapter rule (SPEC-E7-E8 §2.1): CLI and MCP are thin transports over one core, and
both emit a byte-identical core payload.

Tests that drive the CLI must be synchronous: ``canonic query``/``canonic sql`` call
``asyncio.run(...)`` internally, which cannot run inside an active event loop. The
MCP side of those tests is therefore invoked via ``asyncio.run`` rather than an
``async def`` test.
"""

from __future__ import annotations

import json
from pathlib import Path  # noqa: TC003 — used at runtime in fixtures
from typing import TYPE_CHECKING, Any

import pytest
from fastmcp import Client
from typer.testing import CliRunner

from canonic.cli.app import app
from canonic.config import CanonicConfig
from canonic.contracts.models import CanonicalRef, MetricBinding
from canonic.contracts.resolver import ContractResolver
from canonic.core.service import CanonicService
from canonic.mcp.server import build_server

from .conftest import EXPECTED_REVENUE

if TYPE_CHECKING:
    from collections.abc import Mapping

pytestmark = pytest.mark.integration

_REVENUE_QUERY: dict[str, Any] = {"metrics": ["revenue"]}


def _canonical(payload: Any) -> str:
    """Canonical JSON for byte-identity comparison (Decimal/None handled via str)."""
    return json.dumps(payload, sort_keys=True, default=str)


async def _mcp_call(service: CanonicService, tool: str, args: Mapping[str, Any]) -> Any:
    mcp = build_server(service)
    async with Client(mcp) as client:
        result = await client.call_tool(tool, dict(args))
    return result.data


# ----------------------------------------------------------------------------
# Compile + execute
# ----------------------------------------------------------------------------


def test_compile_revenue(e2e_service: CanonicService) -> None:
    """SQL carries the measure aggregate and the injected guardrail filter."""
    from canonic.compiler import SemanticQuery

    result = e2e_service.compile_query(SemanticQuery(metrics=["revenue"]))
    sql = result.sql
    assert "SUM(" in sql.upper()
    assert "amount" in sql.lower()
    assert "<> 'refunded'" in sql
    assert any(g.id == "revenue-excludes-refunds" for g in result.guardrails_fired)


@pytest.mark.asyncio
async def test_query_revenue(e2e_service: CanonicService) -> None:
    """A live query returns rows, the fired guardrail, and freshness metadata."""
    from canonic.compiler import SemanticQuery

    result = await e2e_service.query(SemanticQuery(metrics=["revenue"]))
    payload = result.model_dump(mode="json")

    assert payload["result"]["rows"], "expected at least one row"
    assert str(payload["result"]["rows"][0][0]) == EXPECTED_REVENUE
    assert any(
        g["id"] == "revenue-excludes-refunds" for g in payload["metadata"]["guardrails_fired"]
    )
    assert payload["metadata"]["freshness"], "expected non-empty freshness metadata"


# ----------------------------------------------------------------------------
# Parity
# ----------------------------------------------------------------------------


def test_parity(e2e_project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """CLI ``--json query`` and the MCP ``query`` tool emit identical payloads."""
    import asyncio

    monkeypatch.chdir(e2e_project)
    query_file = e2e_project / "q.json"
    query_file.write_text(json.dumps(_REVENUE_QUERY))

    cli = CliRunner().invoke(app, ["--json", "query", "-f", str(query_file)])
    assert cli.exit_code == 0, cli.stdout
    cli_payload = json.loads(cli.stdout)

    service = CanonicService.from_project(e2e_project)
    mcp_payload = asyncio.run(_mcp_call(service, "query", {"query": _REVENUE_QUERY}))

    assert _canonical(cli_payload) == _canonical(mcp_payload)
    assert str(cli_payload["result"]["rows"][0][0]) == EXPECTED_REVENUE


# ----------------------------------------------------------------------------
# Read-only enforcement
# ----------------------------------------------------------------------------


def test_read_only_violation(e2e_project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A non-SELECT is rejected with READ_ONLY_VIOLATION on both surfaces."""
    import asyncio

    monkeypatch.chdir(e2e_project)

    cli = CliRunner().invoke(app, ["--json", "sql", "DROP TABLE fct_orders"])
    assert cli.exit_code == 11
    assert json.loads(cli.stderr)["code"] == "read_only_violation"

    service = CanonicService.from_project(e2e_project)
    mcp_payload = asyncio.run(_mcp_call(service, "run_sql", {"sql": "DROP TABLE fct_orders"}))
    assert mcp_payload["code"] == "read_only_violation"


# ----------------------------------------------------------------------------
# Error contracts
# ----------------------------------------------------------------------------


def test_unresolved(e2e_project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An unknown metric maps to exit 2 (CLI) and UNRESOLVED (MCP)."""
    import asyncio

    monkeypatch.chdir(e2e_project)
    query_file = e2e_project / "q.json"
    query_file.write_text(json.dumps({"metrics": ["does_not_exist"]}))

    cli = CliRunner().invoke(app, ["--json", "query", "-f", str(query_file)])
    assert cli.exit_code == 2
    assert json.loads(cli.stderr)["code"] == "unresolved"

    service = CanonicService.from_project(e2e_project)
    mcp_payload = asyncio.run(
        _mcp_call(service, "query", {"query": {"metrics": ["does_not_exist"]}})
    )
    assert mcp_payload["code"] == "unresolved"


def test_ambiguous(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A name matching two bindings maps to exit 3 (CLI) and AMBIGUOUS + candidates (MCP).

    The contract loader rejects duplicate active names/aliases, so genuine ambiguity
    cannot be loaded from files; the conflicting bindings are constructed in-memory and
    the resolver is injected into both surfaces. This exercises the surface mapping,
    not the (separately unit-tested) resolver.
    """
    import asyncio

    config = CanonicConfig.model_validate(
        {
            "version": 1,
            "project": {"name": "ambiguous"},
            "connections": [
                {
                    "id": "warehouse_pg",
                    "type": "postgres",
                    "params": {},
                    "credentials_ref": "env:UNUSED",
                }
            ],
            "llm": {"provider": "openai_compatible", "base_url": "http://x", "model": "m"},
        }
    )
    ref = CanonicalRef(source="orders", measure="total_revenue")
    bindings = [
        MetricBinding(metric="revenue", canonical=ref, aliases=["rev"], status="active"),
        MetricBinding(metric="gross_revenue", canonical=ref, aliases=["rev"], status="active"),
    ]
    resolver = ContractResolver(bindings=bindings, guardrails=[])
    service = CanonicService(config=config, resolver=resolver, sources=[])

    # MCP surface: AMBIGUOUS with candidates.
    mcp_payload = asyncio.run(_mcp_call(service, "query", {"query": {"metrics": ["rev"]}}))
    assert mcp_payload["code"] == "ambiguous"
    assert len(mcp_payload["candidates"]) == 2

    # CLI surface: exit 3. Inject the ambiguous service via the shared loader, so no
    # on-disk project is needed (the loader rejects ambiguity at load time anyway).
    monkeypatch.setattr("canonic.cli.commands.query.load_service", lambda _ctx: service)
    query_file = tmp_path / "q.json"
    query_file.write_text(json.dumps({"metrics": ["rev"]}))
    cli = CliRunner().invoke(app, ["--json", "query", "-f", str(query_file)])
    assert cli.exit_code == 3
    assert json.loads(cli.stderr)["code"] == "ambiguous"
