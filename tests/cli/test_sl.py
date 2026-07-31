"""Tests for ``canonic sl resolve`` and ``canonic sl compile`` commands (SPEC-E7 §3)."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest
from typer.testing import CliRunner

from canonic.cli.app import app

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
filters:
  - name: revenue-excludes-refunds
    expr: "status <> 'refunded'"
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
    both directly and through ``dim_mid``, so any query grouping by ``target_name`` is
    genuinely ambiguous without ``via``.
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
def runner() -> CliRunner:
    return CliRunner()


class TestSlResolveCommand:
    def test_exits_zero(self, runner: CliRunner, project_dir: Path) -> None:
        result = runner.invoke(app, ["sl", "resolve", "revenue"])
        assert result.exit_code == 0, result.output

    def test_shows_metric_name(self, runner: CliRunner, project_dir: Path) -> None:
        result = runner.invoke(app, ["sl", "resolve", "revenue"])
        assert "revenue" in result.output

    def test_shows_source_and_measure(self, runner: CliRunner, project_dir: Path) -> None:
        result = runner.invoke(app, ["sl", "resolve", "revenue"])
        assert "orders" in result.output
        assert "total_revenue" in result.output

    def test_alias_resolves(self, runner: CliRunner, project_dir: Path) -> None:
        result = runner.invoke(app, ["sl", "resolve", "rev"])
        assert result.exit_code == 0, result.output
        assert "revenue" in result.output

    def test_json_output_valid(self, runner: CliRunner, project_dir: Path) -> None:
        result = runner.invoke(app, ["--json", "sl", "resolve", "revenue"])
        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        assert payload["metric"] == "revenue"
        assert payload["source"] == "orders"
        assert payload["measure"] == "total_revenue"

    def test_json_has_mcp_parity_keys(self, runner: CliRunner, project_dir: Path) -> None:
        result = runner.invoke(app, ["--json", "sl", "resolve", "revenue"])
        payload = json.loads(result.output)
        assert set(payload.keys()) == {"metric", "source", "measure"}

    def test_unresolved_exits_2(self, runner: CliRunner, project_dir: Path) -> None:
        result = runner.invoke(app, ["sl", "resolve", "does_not_exist"])
        assert result.exit_code == 2

    def test_unresolved_json_exits_2(self, runner: CliRunner, project_dir: Path) -> None:
        result = runner.invoke(app, ["--json", "sl", "resolve", "does_not_exist"])
        assert result.exit_code == 2


class TestSlCompileCommand:
    @pytest.fixture
    def query_file(self, tmp_path: Path) -> Path:
        q = tmp_path / "q.json"
        q.write_text(json.dumps({"metrics": ["revenue"], "dimensions": ["order_date"]}))
        return q

    def test_exits_zero(self, runner: CliRunner, project_dir: Path, query_file: Path) -> None:
        result = runner.invoke(app, ["sl", "compile", "-f", str(query_file)])
        assert result.exit_code == 0, result.output

    def test_output_contains_sql(
        self, runner: CliRunner, project_dir: Path, query_file: Path
    ) -> None:
        result = runner.invoke(app, ["sl", "compile", "-f", str(query_file)])
        assert "SELECT" in result.output.upper()

    def test_output_shows_resolved(
        self, runner: CliRunner, project_dir: Path, query_file: Path
    ) -> None:
        result = runner.invoke(app, ["sl", "compile", "-f", str(query_file)])
        assert "revenue" in result.output

    def test_json_output_valid(
        self, runner: CliRunner, project_dir: Path, query_file: Path
    ) -> None:
        result = runner.invoke(app, ["--json", "sl", "compile", "-f", str(query_file)])
        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        assert "compiled" in payload
        assert "metadata" in payload

    def test_json_compiled_has_sql_and_dialect(
        self, runner: CliRunner, project_dir: Path, query_file: Path
    ) -> None:
        result = runner.invoke(app, ["--json", "sl", "compile", "-f", str(query_file)])
        payload = json.loads(result.output)
        assert "sql" in payload["compiled"]
        assert "dialect" in payload["compiled"]

    def test_json_metadata_has_resolved(
        self, runner: CliRunner, project_dir: Path, query_file: Path
    ) -> None:
        result = runner.invoke(app, ["--json", "sl", "compile", "-f", str(query_file)])
        payload = json.loads(result.output)
        assert "resolved" in payload["metadata"]

    def test_json_mcp_parity(self, runner: CliRunner, project_dir: Path, query_file: Path) -> None:
        """--json payload matches CompileOutput.from_compile_result (adapter parity)."""
        from canonic.compiler import SemanticQuery
        from canonic.core.models import CompileOutput
        from canonic.core.service import CanonicService

        result = runner.invoke(app, ["--json", "sl", "compile", "-f", str(query_file)])
        cli_payload = json.loads(result.output)

        service = CanonicService.from_project(project_dir)
        sq = SemanticQuery.model_validate_json(query_file.read_text())
        expected = CompileOutput.from_compile_result(service.compile_query(sq)).model_dump(
            mode="json"
        )

        assert cli_payload == expected

    def test_unresolved_metric_exits_nonzero(
        self, runner: CliRunner, project_dir: Path, tmp_path: Path
    ) -> None:
        bad = tmp_path / "bad.json"
        bad.write_text(json.dumps({"metrics": ["no_such_metric"]}))
        result = runner.invoke(app, ["sl", "compile", "-f", str(bad)])
        assert result.exit_code != 0

    def test_missing_file_exits_nonzero(self, runner: CliRunner, project_dir: Path) -> None:
        result = runner.invoke(app, ["sl", "compile", "-f", "/nonexistent/q.json"])
        assert result.exit_code != 0

    def test_metrics_dimensions_flags_match_file(
        self, runner: CliRunner, project_dir: Path, query_file: Path
    ) -> None:
        """AC1: flags build the identical SemanticQuery a JSON file would deserialize."""
        via_flags = runner.invoke(
            app, ["--json", "sl", "compile", "--metrics", "revenue", "--dimensions", "order_date"]
        )
        via_file = runner.invoke(app, ["--json", "sl", "compile", "-f", str(query_file)])
        assert via_flags.exit_code == 0, via_flags.output
        assert json.loads(via_flags.output) == json.loads(via_file.output)

    def test_filter_flag_matches_json_filter(
        self, runner: CliRunner, project_dir: Path, tmp_path: Path
    ) -> None:
        """AC2: --filter produces the same filters entry as the equivalent JSON file."""
        via_flag = runner.invoke(
            app, ["--json", "sl", "compile", "--metrics", "revenue", "--filter", "status=active"]
        )
        json_file = tmp_path / "filtered.json"
        json_file.write_text(json.dumps({"metrics": ["revenue"], "filters": ["status = 'active'"]}))
        via_file = runner.invoke(app, ["--json", "sl", "compile", "-f", str(json_file)])
        assert via_flag.exit_code == 0, via_flag.output
        assert json.loads(via_flag.output) == json.loads(via_file.output)

    def test_comma_and_repeated_dimensions_equivalent(
        self, runner: CliRunner, project_dir: Path
    ) -> None:
        """AC3: --dimensions a,b == --dimensions a --dimensions b."""
        comma = runner.invoke(
            app,
            [
                "--json",
                "sl",
                "compile",
                "--metrics",
                "revenue",
                "--dimensions",
                "order_date,status",
            ],
        )
        repeated = runner.invoke(
            app,
            [
                "--json",
                "sl",
                "compile",
                "--metrics",
                "revenue",
                "--dimensions",
                "order_date",
                "--dimensions",
                "status",
            ],
        )
        assert comma.exit_code == 0, comma.output
        assert json.loads(comma.output) == json.loads(repeated.output)

    def test_file_and_metrics_flag_together_is_usage_error(
        self, runner: CliRunner, project_dir: Path, query_file: Path
    ) -> None:
        """AC4: -f plus any of --metrics/--dimensions/--filter is a usage error."""
        result = runner.invoke(
            app, ["sl", "compile", "-f", str(query_file), "--metrics", "revenue"]
        )
        assert result.exit_code != 0

    def test_no_file_and_no_metrics_is_usage_error(
        self, runner: CliRunner, project_dir: Path
    ) -> None:
        result = runner.invoke(app, ["sl", "compile"])
        assert result.exit_code != 0


class TestSlCompileViaAndLimitFlags:
    """``--via`` and ``--limit`` give the inline-flag CLI path the same power the
    ``-f``/JSON path and MCP tools already have (SemanticQuery.via/.limit)."""

    def test_ambiguous_join_without_via_raises(
        self, runner: CliRunner, ambiguous_project_dir: Path
    ) -> None:
        result = runner.invoke(
            app, ["sl", "compile", "--metrics", "amount_total", "--dimensions", "target_name"]
        )
        assert result.exit_code != 0
        assert "ambiguous_join_path" in result.output
        assert '"via"' in result.output

    def test_via_direct_path_resolves(self, runner: CliRunner, ambiguous_project_dir: Path) -> None:
        result = runner.invoke(
            app,
            [
                "sl",
                "compile",
                "--metrics",
                "amount_total",
                "--dimensions",
                "target_name",
                "--via",
                "dim_target",
            ],
        )
        assert result.exit_code == 0, result.output

    def test_via_indirect_path_resolves(
        self, runner: CliRunner, ambiguous_project_dir: Path
    ) -> None:
        result = runner.invoke(
            app,
            [
                "sl",
                "compile",
                "--metrics",
                "amount_total",
                "--dimensions",
                "target_name",
                "--via",
                "dim_mid,dim_target",
            ],
        )
        assert result.exit_code == 0, result.output

    def test_via_flag_matches_json_via(
        self, runner: CliRunner, ambiguous_project_dir: Path, tmp_path: Path
    ) -> None:
        via_flag = runner.invoke(
            app,
            [
                "--json",
                "sl",
                "compile",
                "--metrics",
                "amount_total",
                "--dimensions",
                "target_name",
                "--via",
                "dim_target",
            ],
        )
        json_file = tmp_path / "via.json"
        json_file.write_text(
            json.dumps(
                {"metrics": ["amount_total"], "dimensions": ["target_name"], "via": ["dim_target"]}
            )
        )
        via_file = runner.invoke(app, ["--json", "sl", "compile", "-f", str(json_file)])
        assert via_flag.exit_code == 0, via_flag.output
        assert json.loads(via_flag.output) == json.loads(via_file.output)

    def test_limit_flag_reaches_compiled_sql(self, runner: CliRunner, project_dir: Path) -> None:
        result = runner.invoke(
            app,
            ["sl", "compile", "--metrics", "revenue", "--dimensions", "order_date", "--limit", "5"],
        )
        assert result.exit_code == 0, result.output
        assert "LIMIT 5" in result.output.upper()

    def test_file_and_via_together_is_usage_error(
        self, runner: CliRunner, ambiguous_project_dir: Path, tmp_path: Path
    ) -> None:
        query_file = tmp_path / "q.json"
        query_file.write_text(json.dumps({"metrics": ["amount_total"]}))
        result = runner.invoke(app, ["sl", "compile", "-f", str(query_file), "--via", "dim_target"])
        assert result.exit_code != 0
