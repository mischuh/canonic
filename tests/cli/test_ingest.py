"""Tests for `canonic ingest` (GH-37) — CLI surface over the E4 pipeline (SPEC-E4 §2, §7, §8)."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest
from typer.testing import CliRunner

from canonic.cli.app import app
from canonic.config import CanonicConfig, LLMConfig, ProjectConfig
from canonic.connectors.base import (
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
    credentials_ref: env:CANONIC_PW
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
    """A connector whose introspection raises a raw (non-canonic) transport error."""

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
    """A valid canonic project with one connection; connector resolution is stubbed offline."""
    (tmp_path / "canonic.yaml").write_text(_CONFIG)
    monkeypatch.chdir(tmp_path)

    class _StubFactory:
        def create(self, _conn):
            return _FakeConnector()

    monkeypatch.setattr("canonic.cli.commands.ingest.default_factory", _StubFactory())
    return tmp_path


@pytest.fixture
def publisher(monkeypatch: pytest.MonkeyPatch) -> _RecordingPublisher:
    """Inject a recording publisher so the auto-PR step never shells out to git/gh."""
    pub = _RecordingPublisher()
    monkeypatch.setattr("canonic.cli.commands.ingest.build_publisher", lambda _root: pub)
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

    monkeypatch.setattr("canonic.cli.commands.ingest.default_factory", _UnreachableFactory())

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


# ---------------------------------------------------------------------------
# OB-S6: first_curated_review_completed emitted on first post-setup ingest
# ---------------------------------------------------------------------------


def test_ob_s6_first_curated_review_completed_emitted_after_ingest(project: Path) -> None:
    """first_curated_review_completed is emitted on the first successful (non-dry-run) ingest."""
    from canonic.instrumentation.models import FunnelMilestone
    from canonic.instrumentation.report import read_events

    result = CliRunner().invoke(app, ["ingest", "--bootstrap"])

    assert result.exit_code == 0, result.output
    events = read_events(project, kind="funnel_milestone")
    milestones = [e.milestone for e in events]
    assert FunnelMilestone.FIRST_CURATED_REVIEW_COMPLETED in milestones


def test_ob_s6_first_curated_review_completed_emitted_only_once(project: Path) -> None:
    """A second ingest run does NOT re-emit first_curated_review_completed (once-only guard)."""
    from canonic.instrumentation.models import FunnelMilestone
    from canonic.instrumentation.report import read_events

    CliRunner().invoke(app, ["ingest", "--bootstrap"])
    CliRunner().invoke(app, ["ingest", "--bootstrap"])

    events = read_events(project, kind="funnel_milestone")
    completed = [e for e in events if e.milestone == FunnelMilestone.FIRST_CURATED_REVIEW_COMPLETED]
    assert len(completed) == 1


def test_ob_s6_dry_run_does_not_emit_first_curated_review_completed(project: Path) -> None:
    """--dry-run must NOT emit first_curated_review_completed (nothing reviewed)."""
    from canonic.instrumentation.models import FunnelMilestone
    from canonic.instrumentation.report import read_events

    result = CliRunner().invoke(app, ["ingest", "--dry-run"])

    assert result.exit_code == 0, result.output
    events = read_events(project, kind="funnel_milestone")
    milestones = [e.milestone for e in events]
    assert FunnelMilestone.FIRST_CURATED_REVIEW_COMPLETED not in milestones


# ---------------------------------------------------------------------------
# _wire_extraction_skills — backfills GenericEvidenceConnector's default Null skill
# with the real, config-driven ExtractionSkill without overriding a deliberate choice
# (e.g. Notion's deterministic NotionExtractionSkill) (E3 §5 amendment).
# ---------------------------------------------------------------------------


class _FakeFetchAdapter:
    async def fetch(self) -> list:  # pragma: no cover - never invoked by these tests
        return []


class _StubExtractionSkill:
    async def extract(self, doc, *, source):  # pragma: no cover - never invoked
        raise NotImplementedError


def _config_with_llm() -> CanonicConfig:
    return CanonicConfig(
        version=1,
        project=ProjectConfig(name="t"),
        llm=LLMConfig(
            provider="openai_compatible", base_url="http://localhost:11434/v1", model="small-local"
        ),
    )


def test_wire_extraction_skills_backfills_default_null_skill() -> None:
    from canonic.cli.commands.ingest import _wire_extraction_skills
    from canonic.connectors.evidence import GenericEvidenceConnector, NullExtractionSkill

    connector = GenericEvidenceConnector(_FakeFetchAdapter(), source="confluence_space")

    _wire_extraction_skills({"confluence_space": connector}, _config_with_llm(), headless=False)

    assert not isinstance(connector.extraction_skill, NullExtractionSkill)


def test_wire_extraction_skills_headless_keeps_null_skill() -> None:
    from canonic.cli.commands.ingest import _wire_extraction_skills
    from canonic.connectors.evidence import GenericEvidenceConnector, NullExtractionSkill

    connector = GenericEvidenceConnector(_FakeFetchAdapter(), source="confluence_space")

    _wire_extraction_skills({"confluence_space": connector}, _config_with_llm(), headless=True)

    assert isinstance(connector.extraction_skill, NullExtractionSkill)


def test_wire_extraction_skills_never_overrides_explicit_skill() -> None:
    """Notion's deterministic NotionExtractionSkill (or any explicit skill) is never replaced."""
    from canonic.cli.commands.ingest import _wire_extraction_skills
    from canonic.connectors.evidence import GenericEvidenceConnector

    explicit_skill = _StubExtractionSkill()
    connector = GenericEvidenceConnector(
        _FakeFetchAdapter(), source="notion_wiki", extraction_skill=explicit_skill
    )

    _wire_extraction_skills({"notion_wiki": connector}, _config_with_llm(), headless=False)

    assert connector.extraction_skill is explicit_skill


def test_wire_extraction_skills_ignores_non_evidence_connectors() -> None:
    from canonic.cli.commands.ingest import _wire_extraction_skills

    # Must not raise even though _FakeConnector has no extraction_skill concept at all.
    _wire_extraction_skills({"warehouse_pg": _FakeConnector()}, _config_with_llm(), headless=False)
