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


_FCT_YAML = """\
name: fct
connection: warehouse_pg
table: analytics.fct
grain: [fct_id]
columns:
  - { name: fct_id,    type: string,  nullable: false }
  - { name: mid_id,    type: string,  nullable: false }
  - { name: target_id, type: string,  nullable: false }
  - { name: amount,    type: decimal, nullable: false }
measures:
  - name: total_amount
    expr: "sum(amount)"
    additivity: additive
joins:
  - to: dim_mid
    on: "fct.mid_id = dim_mid.mid_id"
    relationship: many_to_one
  - to: dim_target
    on: "fct.target_id = dim_target.target_id"
    relationship: many_to_one
"""

_DIM_MID_YAML = """\
name: dim_mid
connection: warehouse_pg
table: analytics.dim_mid
grain: [mid_id]
columns:
  - { name: mid_id,    type: string, nullable: false }
  - { name: target_id, type: string, nullable: false }
joins:
  - to: dim_target
    on: "dim_mid.target_id = dim_target.target_id"
    relationship: many_to_one
"""

_DIM_TARGET_YAML = """\
name: dim_target
connection: warehouse_pg
table: analytics.dim_target
grain: [target_id]
columns:
  - { name: target_id,   type: string, nullable: false }
  - { name: target_name, type: string, nullable: false }
dimensions:
  - { name: target_name, column: target_name }
"""

_AMOUNT_TOTAL_YAML = """\
metric: amount_total
canonical:
  source: fct
  measure: total_amount
status: active
"""


@pytest.fixture
def ambiguous_project_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A metric whose source reaches a shared dimension by two distinct join routes.

    Mirrors the real ``examples/saas-analytics`` shape (``fct_opportunities`` reaching
    ``dim_sales_rep`` both directly and via ``dim_customer``): ``fct`` joins ``dim_target``
    both directly and through ``dim_mid``, so a query grouping by ``target_name`` is
    genuinely ambiguous without ``--via``.
    """
    (tmp_path / "canonic.yaml").write_text(_CONFIG)
    sem = tmp_path / "semantics" / "warehouse_pg"
    sem.mkdir(parents=True)
    (sem / "fct.yaml").write_text(_FCT_YAML)
    (sem / "dim_mid.yaml").write_text(_DIM_MID_YAML)
    (sem / "dim_target.yaml").write_text(_DIM_TARGET_YAML)
    contracts = tmp_path / "contracts" / "metrics"
    contracts.mkdir(parents=True)
    (contracts / "amount_total.yaml").write_text(_AMOUNT_TOTAL_YAML)
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


def test_query_limit_flag_accepted(
    runner: CliRunner, project_dir: Path, fake_connector: None
) -> None:
    result = runner.invoke(app, ["query", "--metrics", "revenue", "--limit", "5"])
    assert result.exit_code == 0, result.output


def test_query_ambiguous_join_without_via_raises(
    runner: CliRunner, ambiguous_project_dir: Path, fake_connector: None
) -> None:
    """Mirrors the real-world case: no CLI-flag way to disambiguate meant `via` was
    only reachable through -f/--file or MCP, never inline --metrics/--dimensions."""
    result = runner.invoke(
        app, ["query", "--metrics", "amount_total", "--dimensions", "target_name"]
    )
    assert result.exit_code != 0
    assert "ambiguous_join_path" in result.output


def test_query_tenant_flag_warns_and_succeeds(
    runner: CliRunner, project_dir: Path, fake_connector: None
) -> None:
    """--tenant (SPEC-E12 §5, §7) is accepted for direct CLI use and always warns."""
    result = runner.invoke(app, ["query", "--metrics", "revenue", "--tenant", "4711"])
    assert result.exit_code == 0, result.output
    assert "warning" in result.output.lower()
    assert "4711" in result.output


def test_query_via_flag_resolves_ambiguous_join(
    runner: CliRunner, ambiguous_project_dir: Path, fake_connector: None
) -> None:
    result = runner.invoke(
        app,
        [
            "query",
            "--metrics",
            "amount_total",
            "--dimensions",
            "target_name",
            "--via",
            "dim_target",
        ],
    )
    assert result.exit_code == 0, result.output
