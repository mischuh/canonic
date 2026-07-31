"""Tests for ``canonic query`` (SPEC-E7 §3) — the primary user-facing serving command.

Uses a fake connector (mirrors ``tests/core/test_assertions.py``) so the compile→execute
path runs for real without a live database: only the connector's network call is stubbed.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest
from typer.testing import CliRunner

import canonic.core.context as context_mod
from canonic.cli.app import app
from canonic.connectors.base import Capability, ConnectorBase, Health, ResultColumn, ResultSet

if TYPE_CHECKING:
    from pathlib import Path

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
  - { name: created_at, type: timestamp, nullable: false }
measures:
  - name: total_revenue
    expr: "sum(amount)"
    additivity: additive
dimensions:
  - { name: order_date, column: created_at }
  - { name: status, column: status }
"""

_REVENUE_YAML = """\
metric: revenue
canonical:
  source: orders
  measure: total_revenue
aliases: ["rev"]
status: active
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
        columns=[ResultColumn(name="total_revenue", type="decimal")],
        rows=[[1234.5]],
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


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


def test_query_with_metrics_flag_exits_zero(
    runner: CliRunner, project_dir: Path, fake_connector: None
) -> None:
    result = runner.invoke(app, ["query", "--metrics", "revenue"])
    assert result.exit_code == 0, result.output


def test_query_renders_table_with_column_and_value(
    runner: CliRunner, project_dir: Path, fake_connector: None
) -> None:
    result = runner.invoke(app, ["query", "--metrics", "revenue"])
    assert "total_revenue" in result.output
    assert "1234.5" in result.output


def test_query_with_dimensions_and_filter(
    runner: CliRunner, project_dir: Path, fake_connector: None
) -> None:
    result = runner.invoke(
        app,
        ["query", "--metrics", "revenue", "--dimensions", "order_date", "--filter", "status=paid"],
    )
    assert result.exit_code == 0, result.output


def test_query_json_output_shape(
    runner: CliRunner, project_dir: Path, fake_connector: None
) -> None:
    result = runner.invoke(app, ["--json", "query", "--metrics", "revenue"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["result"]["columns"][0]["name"] == "total_revenue"
    assert payload["result"]["rows"] == [[1234.5]]


def test_query_with_file_option(runner: CliRunner, project_dir: Path, fake_connector: None) -> None:
    query_file = project_dir / "q.json"
    query_file.write_text(json.dumps({"metrics": ["revenue"]}))
    result = runner.invoke(app, ["query", "-f", str(query_file)])
    assert result.exit_code == 0, result.output


def test_query_unresolved_metric_exits_nonzero(
    runner: CliRunner, project_dir: Path, fake_connector: None
) -> None:
    result = runner.invoke(app, ["query", "--metrics", "does_not_exist"])
    assert result.exit_code != 0
    assert result.exception is None or isinstance(result.exception, SystemExit)


def test_query_harness_flag_passes_through_on_no_assertions(
    runner: CliRunner, project_dir: Path, fake_connector: None
) -> None:
    """No assertions are defined for this metric, so --harness is a no-op that still succeeds."""
    result = runner.invoke(app, ["query", "--metrics", "revenue", "--harness"])
    assert result.exit_code == 0, result.output
