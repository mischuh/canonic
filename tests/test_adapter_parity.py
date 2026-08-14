"""Adapter parity — SPEC-P0 §5 item 3.

Verifies that the MCP tools and the direct service-level paths produce
byte-identical core payloads. This is a proxy for the full CLI↔MCP parity gate
(the query/run_sql tools require a live DB connection, so they are covered by
e2e tests).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from fastmcp import Client

from canonic import __version__ as CANONIC_VERSION
from canonic.compiler.query import SemanticQuery
from canonic.contract import CONTRACT_SCHEMA
from canonic.core.models import CompileOutput
from canonic.mcp.server import build_server

if TYPE_CHECKING:
    from pathlib import Path

    from canonic.core.service import CanonicService


@pytest.mark.release_gate
@pytest.mark.asyncio
async def test_resolve_metric_parity(canonic_service: CanonicService) -> None:
    """MCP resolve_metric and direct service path return identical payloads."""
    mcp = build_server(canonic_service)

    async with Client(mcp) as client:
        result = await client.call_tool("resolve_metric", {"name": "revenue"})
    mcp_payload = result.data

    binding = canonic_service.resolve_metric("revenue")
    service_payload = {
        "metric": binding.metric,
        "source": binding.source,
        "measure": binding.measure,
    }

    assert mcp_payload == service_payload


@pytest.mark.release_gate
@pytest.mark.asyncio
async def test_compile_query_parity(canonic_service: CanonicService) -> None:
    """MCP compile_query and direct service path return identical payloads."""
    sq = SemanticQuery(metrics=["revenue"])
    mcp = build_server(canonic_service)

    async with Client(mcp) as client:
        result = await client.call_tool("compile_query", {"query": {"metrics": ["revenue"]}})
    mcp_payload = result.data

    compile_result = canonic_service.compile_query(sq)
    service_payload = CompileOutput.from_compile_result(compile_result).model_dump(mode="json")

    assert mcp_payload == service_payload


@pytest.mark.asyncio
async def test_get_overview_parity(canonic_service: CanonicService) -> None:
    """MCP get_overview and direct service path return identical payloads (AC5)."""
    mcp = build_server(canonic_service)

    async with Client(mcp) as client:
        result = await client.call_tool("get_overview", {})
    mcp_payload = result.data

    service_payload = canonic_service.get_overview().model_dump(mode="json")

    assert mcp_payload == service_payload


@pytest.mark.asyncio
async def test_describe_metric_parity(canonic_service: CanonicService) -> None:
    """MCP describe_metric and direct service path return identical payloads (AC5)."""
    mcp = build_server(canonic_service)

    async with Client(mcp) as client:
        result = await client.call_tool("describe_metric", {"name": "revenue"})
    mcp_payload = result.data

    service_payload = canonic_service.describe_metric("revenue").model_dump(mode="json")

    assert mcp_payload == service_payload


@pytest.mark.asyncio
async def test_contract_info_returns_schema(canonic_service: CanonicService) -> None:
    """contract_info tool returns the contract_schema and running package version."""
    mcp = build_server(canonic_service)
    async with Client(mcp) as client:
        result = await client.call_tool("contract_info", {})
    assert result.data == {
        "contract_schema": CONTRACT_SCHEMA,
        "canonic_version": CANONIC_VERSION,
    }


@pytest.mark.asyncio
async def test_negotiate_contract_accepts_matching_major(canonic_service: CanonicService) -> None:
    mcp = build_server(canonic_service)
    async with Client(mcp) as client:
        result = await client.call_tool("negotiate_contract", {"contract_major": 2})
    assert result.data["accepted"] is True
    assert result.data["contract_schema"] == CONTRACT_SCHEMA


@pytest.mark.asyncio
async def test_negotiate_contract_rejects_mismatched_major(canonic_service: CanonicService) -> None:
    from fastmcp.exceptions import ToolError

    mcp = build_server(canonic_service)
    with pytest.raises(ToolError, match="MAJOR mismatch"):
        async with Client(mcp) as client:
            await client.call_tool("negotiate_contract", {"contract_major": 99})


@pytest.mark.asyncio
async def test_list_reports_parity(canonic_service: CanonicService) -> None:
    """MCP list_reports and direct service path return identical payloads (no project root)."""
    mcp = build_server(canonic_service)

    async with Client(mcp) as client:
        result = await client.call_tool("list_reports", {})
    mcp_payload = result.data

    summaries = canonic_service.list_reports()
    service_payload = {"reports": [s.model_dump(mode="json") for s in summaries]}

    assert mcp_payload == service_payload
    assert mcp_payload == {"reports": []}


@pytest.fixture
def report_project(tmp_path: Path) -> Path:
    """A minimal DuckDB-backed project with one committed report — no live network DB needed.

    Distinct from ``canonic_service`` (root conftest): ``run_report`` executes its
    section's query for real, which needs an actual connection, not just a resolver
    and semantic sources in memory.
    """
    import duckdb

    db_path = tmp_path / "warehouse.duckdb"
    con = duckdb.connect(str(db_path))
    con.execute(
        "CREATE TABLE orders (order_id INTEGER, amount DECIMAL(12,2), status VARCHAR);"
        "INSERT INTO orders VALUES (1, 100.00, 'paid');"
    )
    con.close()

    (tmp_path / "canonic.yaml").write_text(
        "version: 1\n"
        "project:\n  name: test\n  default_connection: warehouse_duckdb\n"
        "connections:\n"
        f"  - id: warehouse_duckdb\n    type: duckdb\n    params: {{path: {db_path}}}\n"
        "llm:\n  provider: openai_compatible\n  base_url: http://localhost/v1\n  model: llama3\n"
    )
    sem = tmp_path / "semantics" / "warehouse_duckdb"
    sem.mkdir(parents=True)
    (sem / "orders.yaml").write_text(
        "name: orders\nconnection: warehouse_duckdb\ntable: orders\ngrain: [order_id]\n"
        "columns:\n  - {name: order_id, type: int, nullable: false}\n"
        "  - {name: amount, type: decimal, nullable: false}\n"
        "  - {name: status, type: string, nullable: false}\n"
        "measures:\n  - {name: total_revenue, expr: 'sum(amount)', additivity: additive}\n"
        "dimensions:\n  - {name: status, column: status}\n"
    )
    metrics = tmp_path / "contracts" / "metrics"
    metrics.mkdir(parents=True)
    (metrics / "revenue.yaml").write_text(
        "metric: revenue\ncanonical:\n  source: orders\n  measure: total_revenue\nstatus: active\n"
    )
    reports = tmp_path / "reports"
    reports.mkdir(parents=True)
    (reports / "customer_report.yaml").write_text(
        "id: customer_report\ntitle: Customer Report\nsections:\n"
        "  - title: Revenue by status\n    query: {metrics: [revenue], dimensions: [status]}\n"
    )
    return tmp_path


def test_run_report_parity(report_project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """S17 AC2: CLI ``report run --json`` and the MCP ``run_report`` tool return
    byte-identical payloads.

    Synchronous, like ``tests/e2e/test_walking_skeleton.py``'s parity tests: ``canonic
    report run`` calls ``asyncio.run(...)`` internally, which cannot run inside an
    already-active event loop, so the MCP side is driven via ``asyncio.run`` from a
    plain ``def`` test rather than an ``async def`` one.
    """
    import asyncio
    import json

    from typer.testing import CliRunner

    from canonic.cli.app import app
    from canonic.core.service import CanonicService

    async def _mcp_run_report(service: CanonicService) -> object:
        mcp = build_server(service)
        async with Client(mcp) as client:
            result = await client.call_tool("run_report", {"report_id": "customer_report"})
        return result.data

    monkeypatch.chdir(report_project)
    cli = CliRunner().invoke(
        app, ["--json", "report", "run", "customer_report"], catch_exceptions=False
    )
    assert cli.exit_code == 0, cli.stdout
    cli_payload = json.loads(cli.stdout)

    service = CanonicService.from_project(report_project)
    mcp_payload = asyncio.run(_mcp_run_report(service))

    assert cli_payload == mcp_payload
    assert cli_payload["sections"][0]["result"]["result"]["rows"] == [["paid", "100.00"]]
