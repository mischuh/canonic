"""Tests for ``canonic overview`` and ``canonic sl describe`` commands (GH-157)."""

from __future__ import annotations

import json
from pathlib import Path  # noqa: TC003 — runtime use in fixture type hints

import pytest
from typer.testing import CliRunner

from canonic.cli.app import app

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
llm:
  provider: openai_compatible
  base_url: http://localhost:11434/v1
  model: llama3
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
def runner() -> CliRunner:
    return CliRunner()


class TestOverviewCommand:
    def test_exits_zero(self, runner: CliRunner, project_dir: Path) -> None:
        result = runner.invoke(app, ["overview"])
        assert result.exit_code == 0, result.output

    def test_shows_domain_and_metric(self, runner: CliRunner, project_dir: Path) -> None:
        result = runner.invoke(app, ["overview"])
        assert "orders" in result.output
        assert "revenue" in result.output

    def test_shows_sample_question(self, runner: CliRunner, project_dir: Path) -> None:
        result = runner.invoke(app, ["overview"])
        assert "?" in result.output

    def test_json_output_valid(self, runner: CliRunner, project_dir: Path) -> None:
        result = runner.invoke(app, ["--json", "overview"])
        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        assert "domains" in payload

    def test_json_domains_have_required_fields(self, runner: CliRunner, project_dir: Path) -> None:
        result = runner.invoke(app, ["--json", "overview"])
        payload = json.loads(result.output)
        for g in payload["domains"]:
            assert "name" in g
            assert "metrics" in g
            assert "sample_questions" in g
            assert g["sample_questions"]
            for m in g["metrics"]:
                assert "name" in m
                assert "label" in m

    def test_domain_filter(self, runner: CliRunner, project_dir: Path) -> None:
        result = runner.invoke(app, ["--json", "overview", "--domain", "orders"])
        payload = json.loads(result.output)
        assert len(payload["domains"]) == 1
        assert payload["domains"][0]["name"] == "orders"

    def test_unknown_domain_empty(self, runner: CliRunner, project_dir: Path) -> None:
        result = runner.invoke(app, ["--json", "overview", "--domain", "nonexistent"])
        payload = json.loads(result.output)
        assert payload["domains"] == []


class TestSlDescribeCommand:
    def test_exits_zero(self, runner: CliRunner, project_dir: Path) -> None:
        result = runner.invoke(app, ["sl", "describe", "revenue"])
        assert result.exit_code == 0, result.output

    def test_shows_metric_name(self, runner: CliRunner, project_dir: Path) -> None:
        result = runner.invoke(app, ["sl", "describe", "revenue"])
        assert "revenue" in result.output

    def test_json_output_valid(self, runner: CliRunner, project_dir: Path) -> None:
        result = runner.invoke(app, ["--json", "sl", "describe", "revenue"])
        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        assert payload["metric"] == "revenue"

    def test_json_includes_examples_field(self, runner: CliRunner, project_dir: Path) -> None:
        result = runner.invoke(app, ["--json", "sl", "describe", "revenue"])
        payload = json.loads(result.output)
        assert "examples" in payload
        assert isinstance(payload["examples"], list)

    def test_alias_lookup(self, runner: CliRunner, project_dir: Path) -> None:
        result = runner.invoke(app, ["--json", "sl", "describe", "rev"])
        payload = json.loads(result.output)
        assert payload["metric"] == "revenue"

    def test_unresolved_exits_nonzero(self, runner: CliRunner, project_dir: Path) -> None:
        result = runner.invoke(app, ["sl", "describe", "mrr"])
        assert result.exit_code != 0
