"""Tests for the ``canon setup`` wizard (GH-15 / SPEC E1 §4)."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from canon.cli.app import app
from canon.config import load_config
from canon.connectors.base import Health

if TYPE_CHECKING:
    from pathlib import Path

    from typer.testing import CliRunner


class _FakeConnector:
    """Stub connector — no live DB; records nothing but the canned results."""

    def __init__(self, health: Health, relations: list[object] | None = None) -> None:
        self._health = health
        self._relations = relations or []

    async def test_connection(self) -> Health:
        return self._health

    async def introspect_schema(self) -> list[object]:
        return self._relations

    async def aclose(self) -> None:
        return None


def _patch_connector(monkeypatch, *connectors: _FakeConnector) -> None:
    """Patch default_factory.create to hand out the given fakes in call order."""
    seq = iter(connectors)

    class _StubFactory:
        def create(self, _conn):
            return next(seq)

    monkeypatch.setattr("canon.cli.commands.setup.default_factory", _StubFactory())


# Prompt answers for a happy-path fresh run (one empty Database/Model overridden).
_FRESH_INPUT = "\n".join(
    [
        "",  # project name → default (cwd name)
        "",  # connection id → warehouse_pg
        "",  # type → postgres
        "",  # host → localhost
        "",  # port → 5432
        "",  # user → postgres
        "analytics",  # database
        "",  # schema (optional)
        "",  # env var → CANON_WAREHOUSE_PG_PASSWORD
        "",  # llm provider → openai_compatible
        "",  # base url
        "llama3",  # model
        "",  # api key env var → none
        "",  # preview schema? → N
    ]
)


def test_fresh_setup_scaffolds_project(runner: CliRunner, tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    _patch_connector(monkeypatch, _FakeConnector(Health(status="ok")))

    result = runner.invoke(app, ["setup"], input=_FRESH_INPUT + "\n")

    assert result.exit_code == 0, result.output
    for name in ("canon.yaml", "semantics", "knowledge", "contracts", "raw-sources"):
        assert (tmp_path / name).exists(), name
    assert (tmp_path / "contracts" / "metrics").is_dir()
    gitignore = (tmp_path / ".gitignore").read_text()
    assert ".canon/" in gitignore
    # The written config re-parses cleanly.
    config = load_config(tmp_path / "canon.yaml")
    assert config.project.name == tmp_path.name
    assert config.connections[0].id == "warehouse_pg"
    assert config.project.default_connection == "warehouse_pg"


def test_secret_indirection_never_literal(runner: CliRunner, tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    _patch_connector(monkeypatch, _FakeConnector(Health(status="ok")))

    runner.invoke(app, ["setup"], input=_FRESH_INPUT + "\n")

    text = (tmp_path / "canon.yaml").read_text()
    # Only the env: indirection ref is written — never an inline credential value.
    assert "credentials_ref: env:CANON_WAREHOUSE_PG_PASSWORD" in text
    assert load_config(tmp_path / "canon.yaml").connections[0].credentials_ref.startswith("env:")


def test_connection_test_gates_recording(runner: CliRunner, tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    # First probe fails, retry succeeds.
    _patch_connector(
        monkeypatch,
        _FakeConnector(Health(status="error", message="boom")),
        _FakeConnector(Health(status="ok")),
    )
    retry_input = "\n".join(
        [
            "",  # project name
            # attempt 1
            "",  # id
            "",  # type
            "",  # host
            "",  # port
            "",  # user
            "db",  # database
            "",  # schema
            "",  # env var
            "",  # Try again? → default yes
            # attempt 2
            "",  # id
            "",  # type
            "",  # host
            "",  # port
            "",  # user
            "db",  # database
            "",  # schema
            "",  # env var
            # llm
            "",  # provider
            "",  # base url
            "m",  # model
            "",  # api key env
            "",  # preview schema?
        ]
    )

    result = runner.invoke(app, ["setup"], input=retry_input + "\n")

    assert result.exit_code == 0, result.output
    assert "connection test failed" in result.output
    config = load_config(tmp_path / "canon.yaml")
    assert len(config.connections) == 1


def test_resume_skips_completed_steps(runner: CliRunner, tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    dotcanon = tmp_path / ".canon"
    dotcanon.mkdir()
    state = {
        "project_name": "resumed",
        "connection": {
            "id": "warehouse_pg",
            "type": "postgres",
            "params": {"host": "h", "port": 5432, "user": "u", "dbname": "db"},
            "credentials_ref": "env:X",
            "read_only_role": None,
        },
        "llm": None,
        "schema_previewed": False,
        "completed_steps": ["name", "connection"],
    }
    (dotcanon / "setup-state.json").write_text(json.dumps(state))
    # default_factory.create must NOT be called — leave it unpatched to catch a stray call.

    resume_input = "\n".join(["", "", "m", "", ""])  # provider, url, model, api key, preview
    result = runner.invoke(app, ["setup"], input=resume_input + "\n")

    assert result.exit_code == 0, result.output
    assert "Connection id" not in result.output  # connection step was skipped
    config = load_config(tmp_path / "canon.yaml")
    assert config.project.name == "resumed"
    assert config.connections[0].id == "warehouse_pg"
    # Checkpoint cleared on success.
    assert not (dotcanon / "setup-state.json").exists()


def test_existing_project_menu_exit_does_not_overwrite(
    runner: CliRunner, project_dir: Path
) -> None:
    before = (project_dir / "canon.yaml").read_bytes()
    result = runner.invoke(app, ["setup"], input="3\n")  # exit immediately
    assert result.exit_code == 0, result.output
    assert "project menu" in result.output
    assert (project_dir / "canon.yaml").read_bytes() == before


def test_existing_project_menu_adds_connection(
    runner: CliRunner, project_dir: Path, monkeypatch
) -> None:
    _patch_connector(monkeypatch, _FakeConnector(Health(status="ok")))
    menu_input = "\n".join(
        [
            "2",  # add connection
            "newconn",  # id
            "",  # type
            "",  # host
            "",  # port
            "",  # user
            "db",  # database
            "",  # schema
            "",  # env var
            "3",  # exit
        ]
    )
    result = runner.invoke(app, ["setup"], input=menu_input + "\n")
    assert result.exit_code == 0, result.output
    config = load_config(project_dir / "canon.yaml")
    assert [c.id for c in config.connections] == ["newconn"]
    assert config.project.name == "test-project"  # untouched


def test_json_mode_rejected(runner: CliRunner, tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["--json", "setup"])
    assert result.exit_code == 1
    assert "interactive" in result.output
    assert not (tmp_path / "canon.yaml").exists()
