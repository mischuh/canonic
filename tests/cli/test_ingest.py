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


class _UnreachableConnector(ConnectorBase):
    """A connector whose introspection raises a raw (non-canon) transport error."""

    def capabilities(self) -> list[Capability]:
        return [Capability.INTROSPECT_SCHEMA, Capability.TEST_CONNECTION]

    async def test_connection(self) -> Health:
        return Health(status="error", message="down")

    async def introspect_schema(self) -> list[RelationSchema]:
        raise RuntimeError("could not connect to server: connection refused")


class _RecordingPublisher:
    """A fake :class:`PullRequestPublisher` that records the git/gh calls instead of running them."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []

    async def create_branch(self, name: str) -> None:
        self.calls.append(("branch", name))

    async def stage(self, paths: object) -> None:
        self.calls.append(("stage", list(paths)))  # type: ignore[arg-type]

    async def commit(self, message: str) -> None:
        self.calls.append(("commit", message))

    async def open_pr(self, title: str, body: str) -> str:
        self.calls.append(("open_pr", title))
        return "https://example.test/pr/1"

    async def comment(self, pr_ref: str, body: str) -> None:
        self.calls.append(("comment", pr_ref))


_CURATED_ORDERS = """\
name: fct_orders
connection: warehouse_pg
table: analytics.fct_orders
grain:
  - order_id
columns:
  - name: order_id
    type: int
    nullable: false
meta:
  provenance: human_curated
  source_fingerprint: sha256:curated-and-different
"""


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A valid canon project with one connection; connector resolution is stubbed offline."""
    (tmp_path / "canon.yaml").write_text(_CONFIG)
    monkeypatch.chdir(tmp_path)

    class _StubFactory:
        def create(self, _conn):
            return _FakeConnector()

    monkeypatch.setattr("canon.cli.commands.ingest.default_factory", _StubFactory())
    return tmp_path


@pytest.fixture
def publisher(monkeypatch: pytest.MonkeyPatch) -> _RecordingPublisher:
    """Inject a recording publisher so the auto-PR step never shells out to git/gh."""
    pub = _RecordingPublisher()
    monkeypatch.setattr("canon.cli.commands.ingest.build_publisher", lambda _root: pub)
    return pub


def _seed_curated_orders(project: Path) -> None:
    """Commit a human_curated fct_orders that conflicts with the inferred evidence (→ contradiction)."""
    target = project / "semantics" / "warehouse_pg" / "fct_orders.yaml"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(_CURATED_ORDERS)


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


def test_unreachable_source_exits_connection_error(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A raw transport failure from the connector becomes CONNECTION_ERROR (exit 13)."""

    class _UnreachableFactory:
        def create(self, _conn):
            return _UnreachableConnector()

    monkeypatch.setattr("canon.cli.commands.ingest.default_factory", _UnreachableFactory())

    result = CliRunner().invoke(app, ["ingest"])

    assert result.exit_code == 13


def test_strict_with_contradiction_exits_14(project: Path) -> None:
    """--strict gates the run on a flagged contradiction with the additive CONTRADICTION code."""
    _seed_curated_orders(project)

    result = CliRunner().invoke(app, ["--json", "ingest", "--strict"])

    assert result.exit_code == 14
    assert json.loads(result.stderr)["code"] == "contradiction"


def test_strict_without_contradiction_exits_0(project: Path) -> None:
    """--strict is a no-op when no contradiction is flagged."""
    result = CliRunner().invoke(app, ["ingest", "--strict", "--no-pr"])

    assert result.exit_code == 0, result.output


def test_headless_opens_auto_pr(project: Path, publisher: _RecordingPublisher) -> None:
    """Headless mode opens an auto-PR carrying the diffs (S9-AC2)."""
    result = CliRunner().invoke(app, ["ingest", "--headless"])

    assert result.exit_code == 0, result.output
    kinds = [call[0] for call in publisher.calls]
    assert kinds == ["branch", "stage", "commit", "open_pr"]


def test_ci_env_auto_detects_headless(
    project: Path, publisher: _RecordingPublisher, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CI=true auto-detects headless mode and opens the auto-PR without the flag."""
    monkeypatch.setenv("CI", "true")

    result = CliRunner().invoke(app, ["ingest"])

    assert result.exit_code == 0, result.output
    assert any(call[0] == "open_pr" for call in publisher.calls)


def test_no_pr_suppresses_auto_pr(project: Path, publisher: _RecordingPublisher) -> None:
    """--no-pr suppresses the auto-PR even in headless mode."""
    result = CliRunner().invoke(app, ["ingest", "--headless", "--no-pr"])

    assert result.exit_code == 0, result.output
    assert publisher.calls == []


def test_dry_run_never_publishes(project: Path, publisher: _RecordingPublisher) -> None:
    """--dry-run never opens a PR, even in headless mode."""
    result = CliRunner().invoke(app, ["ingest", "--headless", "--dry-run"])

    assert result.exit_code == 0, result.output
    assert publisher.calls == []
