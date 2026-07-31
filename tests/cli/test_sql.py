"""Tests for ``canonic sql`` — the read-only SQL escape hatch (E2).

Uses a fake connector (mirrors ``tests/core/test_assertions.py``) so the read-only
execution path runs for real without a live database.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest
from typer.testing import CliRunner

import canonic.core.context as context_mod
from canonic.cli.app import app
from canonic.connectors.base import Capability, ConnectorBase, Health, ResultColumn, ResultSet
from canonic.exc import ReadOnlyViolation

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
  - id: other_pg
    type: postgres
    params: {host: localhost, port: 5432, user: u, dbname: db2}
    credentials_ref: env:CANONIC_PW
"""


class _FakeConnector(ConnectorBase):
    """A read-only connector that returns one canned row, or raises for a bad statement."""

    def __init__(self, result: ResultSet, *, raises: Exception | None = None) -> None:
        self._result = result
        self._raises = raises

    def capabilities(self) -> list[Capability]:
        return [Capability.RUN_READ_ONLY_SQL]

    async def test_connection(self) -> Health:  # pragma: no cover — unused
        return Health(status="ok")

    async def run_read_only_sql(self, sql: str) -> ResultSet:  # noqa: ARG002
        if self._raises is not None:
            raise self._raises
        return self._result

    async def aclose(self) -> None:
        pass


def _one_row_result() -> ResultSet:
    return ResultSet(columns=[ResultColumn(name="n", type="int")], rows=[[1]])


@pytest.fixture
def project_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    (tmp_path / "canonic.yaml").write_text(_CONFIG)
    monkeypatch.setenv("CANONIC_PW", "test")
    monkeypatch.chdir(tmp_path)
    return tmp_path


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


def _patch_connector(monkeypatch: pytest.MonkeyPatch, connector: _FakeConnector) -> None:
    monkeypatch.setattr(context_mod.default_factory, "for_id", lambda *a, **k: connector)  # noqa: ARG005


def test_sql_select_exits_zero(
    runner: CliRunner, project_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_connector(monkeypatch, _FakeConnector(_one_row_result()))
    result = runner.invoke(app, ["sql", "SELECT 1 AS n"])
    assert result.exit_code == 0, result.output


def test_sql_renders_table(
    runner: CliRunner, project_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_connector(monkeypatch, _FakeConnector(_one_row_result()))
    result = runner.invoke(app, ["sql", "SELECT 1 AS n"])
    assert "n" in result.output
    assert "1" in result.output


def test_sql_json_output_shape(
    runner: CliRunner, project_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_connector(monkeypatch, _FakeConnector(_one_row_result()))
    result = runner.invoke(app, ["--json", "sql", "SELECT 1 AS n"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["columns"][0]["name"] == "n"
    assert payload["rows"] == [[1]]


def test_sql_with_explicit_connection_flag(
    runner: CliRunner, project_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_connector(monkeypatch, _FakeConnector(_one_row_result()))
    result = runner.invoke(app, ["sql", "--connection", "other_pg", "SELECT 1 AS n"])
    assert result.exit_code == 0, result.output


def test_sql_rejects_non_select_statement(
    runner: CliRunner, project_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_connector(
        monkeypatch,
        _FakeConnector(_one_row_result(), raises=ReadOnlyViolation("not a SELECT")),
    )
    result = runner.invoke(app, ["sql", "DELETE FROM orders"])
    assert result.exit_code != 0
    assert result.exception is None or isinstance(result.exception, SystemExit)


def test_sql_truncated_shows_note(
    runner: CliRunner, project_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    truncated = ResultSet(columns=[ResultColumn(name="n", type="int")], rows=[[1]], truncated=True)
    _patch_connector(monkeypatch, _FakeConnector(truncated))
    result = runner.invoke(app, ["sql", "SELECT 1 AS n"])
    assert "truncated" in result.output
