"""Tests for `canon ingest` (GH-37) — CLI surface over the E4 pipeline (SPEC-E4 §2, §7, §8)."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest
from typer.testing import CliRunner

from canon.cli.app import app
from canon.connectors.base import (
    Capability,
    ColumnInfo,
    ConnectorBase,
    Health,
    RelationSchema,
    compute_fingerprint,
)

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
    credentials_ref: env:CANON_PW
llm:
  provider: openai_compatible
  base_url: http://localhost:11434/v1
  model: llama3
"""


class _FakeConnector(ConnectorBase):
    def capabilities(self) -> list[Capability]:
        return [Capability.INTROSPECT_SCHEMA, Capability.TEST_CONNECTION]

    async def test_connection(self) -> Health:
        return Health(status="ok")

    async def introspect_schema(self) -> list[RelationSchema]:
        columns = [ColumnInfo(name="order_id", type="int", nullable=False)]
        return [
            RelationSchema(
                connection="warehouse_pg",
                relation="analytics.fct_orders",
                kind="table",
                columns=columns,
                primary_key=["order_id"],
                acquisition_tier="live",
                source_fingerprint=compute_fingerprint(columns, ["order_id"], []),
            )
        ]


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A valid canon project with one connection; connector resolution is stubbed offline."""
    (tmp_path / "canon.yaml").write_text(_CONFIG)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("canon.cli.commands.ingest.connector_for", lambda _conn: _FakeConnector())
    return tmp_path


def test_bootstrap_writes_semantic_files(project: Path) -> None:
    result = CliRunner().invoke(app, ["ingest", "--bootstrap"])

    assert result.exit_code == 0, result.output
    assert (project / "semantics" / "warehouse_pg" / "fct_orders.yaml").exists()


def test_json_emits_structured_report(project: Path) -> None:
    result = CliRunner().invoke(app, ["--json", "ingest", "--dry-run"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert "report" in payload
    assert payload["diffs"][0]["target"] == "semantics/warehouse_pg/fct_orders.yaml"


def test_dry_run_writes_nothing(project: Path) -> None:
    result = CliRunner().invoke(app, ["ingest", "--dry-run"])

    assert result.exit_code == 0, result.output
    assert not (project / "semantics").exists()
    assert not list((project).glob("raw-sources/**/*.jsonl"))


def test_unknown_connection_exits_connection_error(project: Path) -> None:
    result = CliRunner().invoke(app, ["ingest", "--connection", "nope"])

    assert result.exit_code == 13  # CONNECTION_ERROR
