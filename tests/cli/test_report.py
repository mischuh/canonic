"""Tests for ``canonic report`` (AMENDMENT-curated-reports).

Uses the same fake-connector technique as ``tests/cli/test_query.py`` so the
compile -> execute path runs for real without a live database.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

import canonic.core.context as context_mod
from canonic.cli.app import app
from canonic.connectors.base import Capability, ConnectorBase, Health, ResultColumn, ResultSet

if TYPE_CHECKING:
    from pathlib import Path

    from typer.testing import CliRunner

_CONFIG = """\
version: 1
project:
  name: test-project
  default_connection: warehouse_pg
connections:
  - id: warehouse_pg
    type: postgres
    params: {host: localhost, port: 5432, user: u, dbname: db}
    credentials_ref: env:CANONIC_PW
"""

_ORDERS_YAML = """\
name: orders
connection: warehouse_pg
table: analytics.fct_orders
grain: [order_id]
columns:
  - { name: order_id, type: string, nullable: false }
  - { name: amount,   type: decimal, nullable: false }
  - { name: status,   type: string, nullable: false }
measures:
  - name: total_revenue
    expr: "sum(amount)"
    additivity: additive
dimensions:
  - { name: status, column: status }
"""

_REVENUE_YAML = """\
metric: revenue
canonical:
  source: orders
  measure: total_revenue
status: active
"""

_REPORT_YAML = """\
id: customer_report
title: "Customer Report"
description: "desc"
sections:
  - title: "Revenue by status"
    query: { metrics: [revenue], dimensions: [status] }
"""


class _FakeConnector(ConnectorBase):
    """A read-only connector that returns one canned row for every query."""

    def __init__(self, result: ResultSet) -> None:
        self._result = result

    def capabilities(self) -> list[Capability]:
        return [Capability.RUN_READ_ONLY_SQL]

    async def test_connection(self) -> Health:  # pragma: no cover — unused
        return Health(status="ok")

    async def run_read_only_sql(self, sql: str) -> ResultSet:  # noqa: ARG002
        return self._result

    async def aclose(self) -> None:
        pass


def _revenue_result() -> ResultSet:
    return ResultSet(
        columns=[
            ResultColumn(name="status", type="string"),
            ResultColumn(name="total_revenue", type="decimal"),
        ],
        rows=[["paid", 1234.5]],
    )


@pytest.fixture
def project_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    (tmp_path / "canonic.yaml").write_text(_CONFIG)
    sem = tmp_path / "semantics" / "warehouse_pg"
    sem.mkdir(parents=True)
    (sem / "orders.yaml").write_text(_ORDERS_YAML)
    contracts = tmp_path / "contracts" / "metrics"
    contracts.mkdir(parents=True)
    (contracts / "revenue.yaml").write_text(_REVENUE_YAML)
    reports = tmp_path / "reports"
    reports.mkdir(parents=True)
    (reports / "customer_report.yaml").write_text(_REPORT_YAML)
    monkeypatch.setenv("CANONIC_PW", "test")
    monkeypatch.chdir(tmp_path)
    return tmp_path


@pytest.fixture
def fake_connector(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        context_mod.default_factory,
        "for_id",
        lambda *a, **k: _FakeConnector(_revenue_result()),  # noqa: ARG005
    )


def test_bare_report_points_to_audit(runner: CliRunner, project_dir: Path) -> None:
    result = runner.invoke(app, ["report"])
    assert result.exit_code != 0
    assert "canonic audit" in result.output


def test_list_shows_committed_report(runner: CliRunner, project_dir: Path) -> None:
    result = runner.invoke(app, ["report", "list"])
    assert result.exit_code == 0, result.output
    assert "customer_report" in result.output
    assert "Customer Report" in result.output


def test_list_json_output_shape(runner: CliRunner, project_dir: Path) -> None:
    result = runner.invoke(app, ["--json", "report", "list"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload == {
        "reports": [
            {
                "id": "customer_report",
                "title": "Customer Report",
                "description": "desc",
                "owner": None,
                "domain": None,
            }
        ]
    }


def test_list_domain_filter_excludes_non_matching(runner: CliRunner, project_dir: Path) -> None:
    result = runner.invoke(app, ["report", "list", "--domain", "nope"])
    assert result.exit_code == 0, result.output
    assert "no reports found" in result.output.lower()


def test_run_exits_zero_and_renders_table(
    runner: CliRunner, project_dir: Path, fake_connector: None
) -> None:
    result = runner.invoke(app, ["report", "run", "customer_report"])
    assert result.exit_code == 0, result.output
    assert "Revenue by status" in result.output
    assert "1234.5" in result.output


def test_run_json_output_matches_query_result_shape(
    runner: CliRunner, project_dir: Path, fake_connector: None
) -> None:
    result = runner.invoke(app, ["--json", "report", "run", "customer_report"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["report_id"] == "customer_report"
    assert len(payload["sections"]) == 1
    section = payload["sections"][0]
    assert section["title"] == "Revenue by status"
    assert section["error"] is None
    assert section["result"]["result"]["rows"] == [["paid", 1234.5]]


def test_run_unknown_report_id_fails(
    runner: CliRunner, project_dir: Path, fake_connector: None
) -> None:
    result = runner.invoke(app, ["report", "run", "does_not_exist"])
    assert result.exit_code != 0
    assert "unresolved" in result.output.lower() or "does_not_exist" in result.output
