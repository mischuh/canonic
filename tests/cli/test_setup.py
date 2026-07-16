"""Tests for the ``canonic setup`` wizard (GH-15 / SPEC E1 §4)."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest

from canonic.cli.app import app
from canonic.cli.commands._schema_selection import parse_index_ranges, parse_table_tokens
from canonic.config import load_config
from canonic.connectors.base import Health

if TYPE_CHECKING:
    from pathlib import Path

    from typer.testing import CliRunner


def _relation(name: str) -> SimpleNamespace:
    """A minimal stand-in for RelationSchema exposing only the ``.relation`` attribute."""
    return SimpleNamespace(relation=name)


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

    monkeypatch.setattr("canonic.cli.commands.setup.default_factory", _StubFactory())
    monkeypatch.setattr("canonic.cli.commands._schema_selection.default_factory", _StubFactory())


# Prompt answers for a happy-path fresh run using the Postgres path.
_FRESH_INPUT = "\n".join(
    [
        "",  # project name → default (cwd name)
        "3",  # connection type → postgres (1=sqlite, 2=duckdb, 3=postgres)
        "",  # connection id → warehouse_pg
        "",  # host → localhost
        "",  # port → 5432
        "",  # user → postgres
        "analytics",  # database
        "",  # env var → CANONIC_WAREHOUSE_PG_PASSWORD
        "n",  # narrow schemas/tables? → No
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
    for name in ("canonic.yaml", "semantics", "knowledge", "contracts", "raw-sources"):
        assert (tmp_path / name).exists(), name
    assert (tmp_path / "contracts" / "metrics").is_dir()
    gitignore = (tmp_path / ".gitignore").read_text()
    assert ".canonic/" in gitignore
    # The written config re-parses cleanly.
    config = load_config(tmp_path / "canonic.yaml")
    assert config.project.name == tmp_path.name
    assert config.connections[0].id == "warehouse_pg"
    assert config.project.default_connection == "warehouse_pg"


def test_secret_indirection_never_literal(runner: CliRunner, tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    _patch_connector(monkeypatch, _FakeConnector(Health(status="ok")))

    runner.invoke(app, ["setup"], input=_FRESH_INPUT + "\n")

    text = (tmp_path / "canonic.yaml").read_text()
    # Only the env: indirection ref is written — never an inline credential value.
    assert "credentials_ref: env:CANONIC_WAREHOUSE_PG_PASSWORD" in text
    assert load_config(tmp_path / "canonic.yaml").connections[0].credentials_ref.startswith("env:")


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
            "3",  # type → postgres (1=sqlite, 2=duckdb, 3=postgres)
            "",  # id
            "",  # host
            "",  # port
            "",  # user
            "db",  # database
            "",  # env var
            "",  # Try again? → default yes
            # attempt 2
            "3",  # type → postgres (1=sqlite, 2=duckdb, 3=postgres)
            "",  # id
            "",  # host
            "",  # port
            "",  # user
            "db",  # database
            "",  # env var
            "n",  # narrow schemas/tables? → No
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
    config = load_config(tmp_path / "canonic.yaml")
    assert len(config.connections) == 1


def test_declining_narrowing_writes_no_schema_or_table_params(
    runner: CliRunner, tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.chdir(tmp_path)
    _patch_connector(monkeypatch, _FakeConnector(Health(status="ok")))

    result = runner.invoke(app, ["setup"], input=_FRESH_INPUT + "\n")

    assert result.exit_code == 0, result.output
    params = load_config(tmp_path / "canonic.yaml").connections[0].params
    assert "schemas" not in params
    assert "tables" not in params


def test_narrow_to_one_schema(runner: CliRunner, tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    relations = [
        _relation("finance.fact_revenue"),
        _relation("public.orders"),
        _relation("public.customers"),
    ]
    fake = _FakeConnector(Health(status="ok"), relations=relations)
    # One connector creation for the connection test, one for schema discovery.
    _patch_connector(monkeypatch, fake, fake)
    narrow_input = "\n".join(
        [
            "",  # project name
            "3",  # connection type → postgres
            "",  # connection id
            "",  # host
            "",  # port
            "",  # user
            "db",  # database
            "",  # env var
            "",  # narrow schemas/tables? → default Yes
            "2",  # select schemas → schema #2 (sorted: finance=1, public=2)
            "",  # narrow tables too? → default No
            "",  # llm provider
            "",  # base url
            "m",  # model
            "",  # api key env
            "",  # preview schema?
        ]
    )

    result = runner.invoke(app, ["setup"], input=narrow_input + "\n")

    assert result.exit_code == 0, result.output
    params = load_config(tmp_path / "canonic.yaml").connections[0].params
    assert params["schemas"] == ["public"]
    assert "tables" not in params


def test_narrow_tables_with_index_and_glob(runner: CliRunner, tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    relations = [
        _relation("public.orders"),
        _relation("public.fact_sales"),
        _relation("finance.fact_revenue"),
    ]
    fake = _FakeConnector(Health(status="ok"), relations=relations)
    _patch_connector(monkeypatch, fake, fake)
    narrow_input = "\n".join(
        [
            "",  # project name
            "3",  # connection type → postgres
            "",  # connection id
            "",  # host
            "",  # port
            "",  # user
            "db",  # database
            "",  # env var
            "",  # narrow schemas/tables? → default Yes
            "all",  # select schemas → all
            "y",  # narrow tables too? → Yes
            "1,fact_*",  # index 1 + a literal glob token
            "",  # llm provider
            "",  # base url
            "m",  # model
            "",  # api key env
            "",  # preview schema?
        ]
    )

    result = runner.invoke(app, ["setup"], input=narrow_input + "\n")

    assert result.exit_code == 0, result.output
    params = load_config(tmp_path / "canonic.yaml").connections[0].params
    assert "schemas" not in params
    assert params["tables"] == ["public.orders", "fact_*"]


def test_narrow_schema_invalid_index_reprompts(
    runner: CliRunner, tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.chdir(tmp_path)
    relations = [_relation("public.orders"), _relation("finance.fact_revenue")]
    fake = _FakeConnector(Health(status="ok"), relations=relations)
    _patch_connector(monkeypatch, fake, fake)
    narrow_input = "\n".join(
        [
            "",  # project name
            "3",  # connection type → postgres
            "",  # connection id
            "",  # host
            "",  # port
            "",  # user
            "db",  # database
            "",  # env var
            "",  # narrow schemas/tables? → default Yes
            "99",  # out of range → reprompt
            "1",  # valid index
            "",  # narrow tables too? → default No
            "",  # llm provider
            "",  # base url
            "m",  # model
            "",  # api key env
            "",  # preview schema?
        ]
    )

    result = runner.invoke(app, ["setup"], input=narrow_input + "\n")

    assert result.exit_code == 0, result.output
    assert "out of range" in result.output


class TestParseIndexRanges:
    def test_single_indices(self) -> None:
        assert parse_index_ranges("1,3", 5) == {1, 3}

    def test_range(self) -> None:
        assert parse_index_ranges("2-4", 5) == {2, 3, 4}

    def test_mixed(self) -> None:
        assert parse_index_ranges("1,3-4", 5) == {1, 3, 4}

    def test_blank_tokens_ignored(self) -> None:
        assert parse_index_ranges("1,,3", 5) == {1, 3}

    def test_out_of_range_raises(self) -> None:
        with pytest.raises(ValueError, match="out of range"):
            parse_index_ranges("9", 5)

    def test_non_numeric_raises(self) -> None:
        with pytest.raises(ValueError, match="invalid index"):
            parse_index_ranges("abc", 5)

    def test_backwards_range_raises(self) -> None:
        with pytest.raises(ValueError, match="invalid range"):
            parse_index_ranges("5-1", 5)


class TestParseTableTokens:
    def test_index_resolves_to_name(self) -> None:
        names = ["public.orders", "public.customers"]
        assert parse_table_tokens("1", names) == ["public.orders"]

    def test_glob_token_kept_verbatim(self) -> None:
        names = ["public.orders", "public.customers"]
        assert parse_table_tokens("fact_*", names) == ["fact_*"]

    def test_mixed_index_and_glob(self) -> None:
        names = ["public.orders", "public.customers"]
        assert parse_table_tokens("2,fact_*", names) == ["public.customers", "fact_*"]

    def test_range_resolves_to_multiple_names(self) -> None:
        names = ["a.t1", "a.t2", "a.t3"]
        assert parse_table_tokens("1-2", names) == ["a.t1", "a.t2"]

    def test_duplicate_tokens_deduplicated(self) -> None:
        names = ["a.t1", "a.t2"]
        assert parse_table_tokens("1,1,a.t1", names) == ["a.t1"]


def test_resume_skips_completed_steps(runner: CliRunner, tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    dotcanonic = tmp_path / ".canonic"
    dotcanonic.mkdir()
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
    (dotcanonic / "setup-state.json").write_text(json.dumps(state))
    # default_factory.create must NOT be called — leave it unpatched to catch a stray call.

    resume_input = "\n".join(["", "", "m", "", ""])  # provider, url, model, api key, preview
    result = runner.invoke(app, ["setup"], input=resume_input + "\n")

    assert result.exit_code == 0, result.output
    assert "Connection id" not in result.output  # connection step was skipped
    config = load_config(tmp_path / "canonic.yaml")
    assert config.project.name == "resumed"
    assert config.connections[0].id == "warehouse_pg"
    # Checkpoint cleared on success.
    assert not (dotcanonic / "setup-state.json").exists()


def test_existing_project_menu_exit_does_not_overwrite(
    runner: CliRunner, project_dir: Path
) -> None:
    before = (project_dir / "canonic.yaml").read_bytes()
    result = runner.invoke(app, ["setup"], input="4\n")  # exit immediately
    assert result.exit_code == 0, result.output
    assert "project menu" in result.output
    assert (project_dir / "canonic.yaml").read_bytes() == before


def test_existing_project_menu_adds_connection(
    runner: CliRunner, project_dir: Path, monkeypatch
) -> None:
    _patch_connector(monkeypatch, _FakeConnector(Health(status="ok")))
    menu_input = "\n".join(
        [
            "2",  # add connection
            "3",  # connection type → postgres (1=sqlite, 2=duckdb, 3=postgres)
            "newconn",  # id
            "",  # host
            "",  # port
            "",  # user
            "db",  # database
            "",  # env var
            "n",  # narrow schemas/tables? → No
            "4",  # exit
        ]
    )
    result = runner.invoke(app, ["setup"], input=menu_input + "\n")
    assert result.exit_code == 0, result.output
    config = load_config(project_dir / "canonic.yaml")
    assert [c.id for c in config.connections] == ["newconn"]
    assert config.project.name == "test-project"  # untouched


def test_sqlite_connection_path(runner: CliRunner, tmp_path: Path, monkeypatch) -> None:
    """SQLite path records a connection with no credentials_ref."""
    monkeypatch.chdir(tmp_path)
    db_file = tmp_path / "data.db"
    _patch_connector(monkeypatch, _FakeConnector(Health(status="ok")))
    sqlite_input = "\n".join(
        [
            "",  # project name
            "1",  # connection type → sqlite
            "local_sqlite",  # id
            str(db_file),  # path to .db file
            "",  # llm provider
            "",  # base url
            "m",  # model
            "",  # api key env
            "",  # preview schema?
        ]
    )
    result = runner.invoke(app, ["setup"], input=sqlite_input + "\n")
    assert result.exit_code == 0, result.output
    config = load_config(tmp_path / "canonic.yaml")
    conn = config.connections[0]
    assert conn.id == "local_sqlite"
    assert conn.type == "sqlite"
    assert conn.params["path"] == str(db_file)
    assert conn.credentials_ref is None


def test_existing_project_menu_generates_contracts(runner: CliRunner, project_dir: Path) -> None:
    """Option 3 in the existing-project menu writes inferred contracts from sources."""
    # Write a minimal semantic source YAML with a numeric column and no measures.
    semantics_dir = project_dir / "semantics" / "wh"
    semantics_dir.mkdir(parents=True)
    (semantics_dir / "orders.yaml").write_text(
        "name: orders\n"
        "connection: wh\n"
        "table: orders\n"
        "grain: [id]\n"
        "columns:\n"
        "  - {name: id, type: int, nullable: false}\n"
        "  - {name: amount, type: float, nullable: true}\n"
        "measures: []\n"
        "dimensions: []\n"
    )

    result = runner.invoke(app, ["setup"], input="3\n4\n")
    assert result.exit_code == 0, result.output
    assert "wrote" in result.output
    assert (project_dir / "contracts" / "metrics" / "row-count.yaml").exists()
    assert (project_dir / "contracts" / "metrics" / "total-amount.yaml").exists()


def test_json_mode_rejected(runner: CliRunner, tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["--json", "setup"])
    assert result.exit_code == 1
    assert "interactive" in result.output
    assert not (tmp_path / "canonic.yaml").exists()
