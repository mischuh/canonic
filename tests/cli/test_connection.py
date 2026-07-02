"""Tests for ``canonic connection`` subcommands: list, test, add, remove."""

from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from canonic.cli.app import app

_BASE_CONFIG = """\
version: 1
project:
  name: test-project
  default_connection: db1
connections:
  - id: db1
    type: sqlite
    params:
      path: ":memory:"
  - id: db2
    type: sqlite
    params:
      path: ":memory:"
"""

_NO_CONNECTIONS_CONFIG = """\
version: 1
project:
  name: test-project
"""


@pytest.fixture
def project_dir(tmp_path, monkeypatch):
    (tmp_path / "canonic.yaml").write_text(_BASE_CONFIG)
    monkeypatch.chdir(tmp_path)
    return tmp_path


@pytest.fixture
def empty_project_dir(tmp_path, monkeypatch):
    (tmp_path / "canonic.yaml").write_text(_NO_CONNECTIONS_CONFIG)
    monkeypatch.chdir(tmp_path)
    return tmp_path


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


class TestConnectionList:
    def test_list_shows_connections(self, runner, project_dir):
        result = runner.invoke(app, ["connection", "list"])
        assert result.exit_code == 0
        assert "db1" in result.output
        assert "db2" in result.output

    def test_list_json_mode(self, runner, project_dir):
        result = runner.invoke(app, ["--json", "connection", "list"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        ids = [c["id"] for c in data["connections"]]
        assert ids == ["db1", "db2"]
        assert data["connections"][0]["default"] is True
        assert data["connections"][1]["default"] is False

    def test_list_empty(self, runner, empty_project_dir):
        result = runner.invoke(app, ["connection", "list"])
        assert result.exit_code == 0
        assert "no connections configured" in result.output

    def test_list_outside_project(self, runner, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        result = runner.invoke(app, ["connection", "list"])
        assert result.exit_code == 1


class TestConnectionTest:
    def test_test_all_connections_ok(self, runner, project_dir):
        result = runner.invoke(app, ["connection", "test"])
        assert result.exit_code == 0
        assert "ok" in result.output

    def test_test_specific_connection(self, runner, project_dir):
        result = runner.invoke(app, ["connection", "test", "--connection", "db1"])
        assert result.exit_code == 0
        assert "db1" in result.output
        assert "ok" in result.output

    def test_test_json_mode(self, runner, project_dir):
        result = runner.invoke(app, ["--json", "connection", "test"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert all(r["status"] == "ok" for r in data["results"])

    def test_test_unknown_connection_exits_nonzero(self, runner, project_dir):
        result = runner.invoke(app, ["connection", "test", "--connection", "doesnotexist"])
        assert result.exit_code == 1

    def test_test_no_connections_exits_nonzero(self, runner, empty_project_dir):
        result = runner.invoke(app, ["connection", "test"])
        assert result.exit_code == 1

    def test_test_bad_path_reports_error(self, runner, tmp_path, monkeypatch):
        cfg = tmp_path / "canonic.yaml"
        cfg.write_text(
            "version: 1\nproject:\n  name: t\nconnections:\n"
            "  - id: bad\n    type: sqlite\n    params:\n      path: /no/such/file.db\n"
        )
        monkeypatch.chdir(tmp_path)
        result = runner.invoke(app, ["--json", "connection", "test"])
        assert result.exit_code == 1
        data = json.loads(result.output)
        assert any(r["status"] == "error" for r in data["results"])


class TestConnectionAdd:
    def test_add_new_connection(self, runner, project_dir):
        result = runner.invoke(
            app,
            ["connection", "add", "--id", "db3", "--type", "sqlite", "--param", "path=:memory:"],
        )
        assert result.exit_code == 0
        assert "db3" in result.output
        # Verify it appears in list
        list_result = runner.invoke(app, ["--json", "connection", "list"])
        data = json.loads(list_result.output)
        ids = [c["id"] for c in data["connections"]]
        assert "db3" in ids

    def test_add_with_set_default(self, runner, project_dir):
        result = runner.invoke(
            app,
            [
                "connection",
                "add",
                "--id",
                "newdefault",
                "--type",
                "sqlite",
                "--param",
                "path=:memory:",
                "--set-default",
            ],
        )
        assert result.exit_code == 0
        list_result = runner.invoke(app, ["--json", "connection", "list"])
        data = json.loads(list_result.output)
        defaults = [c for c in data["connections"] if c["default"]]
        assert len(defaults) == 1
        assert defaults[0]["id"] == "newdefault"

    def test_add_duplicate_id_exits_nonzero(self, runner, project_dir):
        result = runner.invoke(app, ["connection", "add", "--id", "db1", "--type", "sqlite"])
        assert result.exit_code == 1

    def test_add_bad_param_format_exits_nonzero(self, runner, project_dir):
        result = runner.invoke(
            app, ["connection", "add", "--id", "db3", "--type", "sqlite", "--param", "badparam"]
        )
        assert result.exit_code == 1

    def test_add_json_mode(self, runner, project_dir):
        result = runner.invoke(
            app, ["--json", "connection", "add", "--id", "db3", "--type", "sqlite"]
        )
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["added"] == "db3"

    def test_add_outside_project(self, runner, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        result = runner.invoke(app, ["connection", "add", "--id", "x", "--type", "sqlite"])
        assert result.exit_code == 1


class TestConnectionRemove:
    def test_remove_existing_connection(self, runner, project_dir):
        result = runner.invoke(app, ["connection", "remove", "db2"])
        assert result.exit_code == 0
        assert "db2" in result.output
        list_result = runner.invoke(app, ["--json", "connection", "list"])
        data = json.loads(list_result.output)
        ids = [c["id"] for c in data["connections"]]
        assert "db2" not in ids

    def test_remove_unknown_connection_exits_nonzero(self, runner, project_dir):
        result = runner.invoke(app, ["connection", "remove", "doesnotexist"])
        assert result.exit_code == 1

    def test_remove_default_connection_warns(self, runner, project_dir):
        result = runner.invoke(app, ["connection", "remove", "db1"])
        assert result.exit_code == 0
        assert "was the default" in result.output

    def test_remove_json_mode(self, runner, project_dir):
        result = runner.invoke(app, ["--json", "connection", "remove", "db2"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["removed"] == "db2"
        assert data["was_default"] is False

    def test_remove_default_clears_default_in_yaml(self, runner, project_dir):
        runner.invoke(app, ["connection", "remove", "db1"])
        list_result = runner.invoke(app, ["--json", "connection", "list"])
        data = json.loads(list_result.output)
        assert all(not c["default"] for c in data["connections"])

    def test_remove_outside_project(self, runner, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        result = runner.invoke(app, ["connection", "remove", "db1"])
        assert result.exit_code == 1
