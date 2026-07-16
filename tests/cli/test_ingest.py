"""Tests for `canonic ingest` (GH-37) — CLI surface over the E4 pipeline (SPEC-E4 §2, §7, §8)."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest
from typer.testing import CliRunner

from canonic.cli.app import app
from canonic.config import CanonicConfig, Connection, LLMConfig, ProjectConfig, load_config
from canonic.connectors.base import (
    Capability,
    ColumnInfo,
    ConnectorBase,
    Health,
    RelationSchema,
    compute_fingerprint,
)
from canonic.exc import ConnectionError as CanonicConnectionError

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


class _MultiRelationConnector(ConnectorBase):
    """A connector exposing two relations: one already curated, one not yet imported."""

    def capabilities(self) -> list[Capability]:
        return [Capability.INTROSPECT_SCHEMA, Capability.TEST_CONNECTION]

    async def test_connection(self) -> Health:
        return Health(status="ok")

    async def introspect_schema(self) -> list[RelationSchema]:
        order_columns = [ColumnInfo(name="order_id", type="int", nullable=False)]
        shipment_columns = [ColumnInfo(name="shipment_id", type="int", nullable=False)]
        return [
            RelationSchema(
                connection="warehouse_pg",
                relation="analytics.fct_orders",
                kind="table",
                columns=order_columns,
                primary_key=["order_id"],
                acquisition_tier="live",
                source_fingerprint=compute_fingerprint(order_columns, ["order_id"], []),
            ),
            RelationSchema(
                connection="warehouse_pg",
                relation="analytics.fct_shipments",
                kind="table",
                columns=shipment_columns,
                primary_key=["shipment_id"],
                acquisition_tier="live",
                source_fingerprint=compute_fingerprint(shipment_columns, ["shipment_id"], []),
            ),
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
def multi_relation_project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A project whose connection exposes two relations: fct_orders and fct_shipments.

    ``add-tables`` discovers relations through ``_schema_selection.default_factory`` (its own
    module-level import), separate from the pipeline's ``ingest.default_factory`` — both must
    be stubbed for a full add-tables run.
    """
    (tmp_path / "canonic.yaml").write_text(_CONFIG)
    monkeypatch.chdir(tmp_path)

    class _StubFactory:
        def create(self, _conn):
            return _MultiRelationConnector()

    monkeypatch.setattr("canonic.cli.commands.ingest.default_factory", _StubFactory())
    monkeypatch.setattr("canonic.cli.commands._schema_selection.default_factory", _StubFactory())
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


def _seed_curated_orders_matching(project: Path) -> None:
    """Commit a human_curated fct_orders whose fingerprint matches the live evidence (→ no-op).

    Used where a test only cares that an already-curated table is excluded/left alone, not
    about contradiction handling — a mismatched fingerprint would otherwise route the run
    through the LLM-backed reconcile drafter.
    """
    fingerprint = compute_fingerprint(
        [ColumnInfo(name="order_id", type="int", nullable=False)], ["order_id"], []
    )
    target = project / "semantics" / "warehouse_pg" / "fct_orders.yaml"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        "name: fct_orders\n"
        "connection: warehouse_pg\n"
        "table: analytics.fct_orders\n"
        "grain:\n"
        "  - order_id\n"
        "columns:\n"
        "  - name: order_id\n"
        "    type: int\n"
        "    nullable: false\n"
        "meta:\n"
        "  provenance: human_curated\n"
        f"  source_fingerprint: {fingerprint}\n"
    )


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
# `canonic ingest add-tables` — widen a connection's table scope interactively,
# excluding tables already curated under semantics/ for that connection.
# ---------------------------------------------------------------------------

_CURATED_SHIPMENTS = """\
name: fct_shipments
connection: warehouse_pg
table: analytics.fct_shipments
grain:
  - shipment_id
columns:
  - name: shipment_id
    type: int
    nullable: false
meta:
  provenance: human_curated
"""

_CONFIG_FILTERED = """\
version: 1
project:
  name: test-project
  default_connection: warehouse_pg
connections:
  - id: warehouse_pg
    type: postgres
    params: {host: localhost, port: 5432, user: u, dbname: db, tables: [analytics.fct_orders]}
    credentials_ref: env:CANONIC_PW
llm:
  provider: openai_compatible
  base_url: http://localhost:11434/v1
  model: llama3
"""


def test_add_tables_still_reachable_as_default_ingest(project: Path) -> None:
    """Converting ingest into a Typer subapp must not break the bare `canonic ingest` run."""
    result = CliRunner().invoke(app, ["ingest", "--bootstrap"])

    assert result.exit_code == 0, result.output
    assert (project / "semantics" / "warehouse_pg" / "fct_orders.yaml").exists()


def test_add_tables_excludes_already_imported(
    multi_relation_project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A table already curated for this connection is excluded from the picker list."""
    monkeypatch.setenv("CI", "true")  # deterministic drafting — join inference needs no live LLM
    _seed_curated_orders_matching(multi_relation_project)

    result = CliRunner().invoke(
        app,
        ["ingest", "add-tables", "--connection", "warehouse_pg", "--dry-run"],
        input="all\nn\n",
    )

    assert result.exit_code == 0, result.output
    assert "1 table(s) already in the semantic model" in result.output
    assert "fct_shipments" in result.output


def test_add_tables_nothing_to_add_when_all_curated(multi_relation_project: Path) -> None:
    """Once every discovered table is curated, add-tables reports nothing to add."""
    _seed_curated_orders(multi_relation_project)
    target = multi_relation_project / "semantics" / "warehouse_pg" / "fct_shipments.yaml"
    target.write_text(_CURATED_SHIPMENTS)

    result = CliRunner().invoke(app, ["ingest", "add-tables", "--connection", "warehouse_pg"])

    assert result.exit_code == 0, result.output
    assert "nothing to add" in result.output


def test_add_tables_merges_into_existing_filter_and_persists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Widening an already-narrowed connection unions and persists the tables filter."""
    monkeypatch.setenv("CI", "true")  # deterministic drafting — join inference needs no live LLM
    (tmp_path / "canonic.yaml").write_text(_CONFIG_FILTERED)
    monkeypatch.chdir(tmp_path)

    class _StubFactory:
        def create(self, _conn):
            return _MultiRelationConnector()

    monkeypatch.setattr("canonic.cli.commands.ingest.default_factory", _StubFactory())
    monkeypatch.setattr("canonic.cli.commands._schema_selection.default_factory", _StubFactory())
    _seed_curated_orders_matching(tmp_path)

    result = CliRunner().invoke(
        app,
        ["ingest", "add-tables", "--connection", "warehouse_pg", "--dry-run"],
        input="all\nn\n",
    )

    assert result.exit_code == 0, result.output
    config = load_config(tmp_path / "canonic.yaml")
    conn = next(c for c in config.connections if c.id == "warehouse_pg")
    assert conn.params["tables"] == ["analytics.fct_orders", "analytics.fct_shipments"]


def test_add_tables_unfiltered_connection_does_not_persist(
    multi_relation_project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unfiltered connection's canonic.yaml stays untouched — the run is scoped transiently."""
    monkeypatch.setenv("CI", "true")  # deterministic drafting — join inference needs no live LLM
    _seed_curated_orders_matching(multi_relation_project)

    result = CliRunner().invoke(
        app,
        ["ingest", "add-tables", "--connection", "warehouse_pg", "--dry-run"],
        input="all\nn\n",
    )

    assert result.exit_code == 0, result.output
    config = load_config(multi_relation_project / "canonic.yaml")
    conn = next(c for c in config.connections if c.id == "warehouse_pg")
    assert "tables" not in conn.params


def test_resolve_one_connection_single_default() -> None:
    from canonic.cli.commands.ingest import _resolve_one_connection

    config = CanonicConfig(
        version=1,
        project=ProjectConfig(name="t"),
        connections=[Connection(id="only", type="sqlite", params={})],
    )
    assert _resolve_one_connection(config, None).id == "only"


def test_resolve_one_connection_explicit_id() -> None:
    from canonic.cli.commands.ingest import _resolve_one_connection

    config = CanonicConfig(
        version=1,
        project=ProjectConfig(name="t"),
        connections=[
            Connection(id="a", type="sqlite", params={}),
            Connection(id="b", type="sqlite", params={}),
        ],
    )
    assert _resolve_one_connection(config, "b").id == "b"


def test_resolve_one_connection_unknown_id_raises() -> None:
    from canonic.cli.commands.ingest import _resolve_one_connection

    config = CanonicConfig(
        version=1,
        project=ProjectConfig(name="t"),
        connections=[Connection(id="a", type="sqlite", params={})],
    )
    with pytest.raises(CanonicConnectionError):
        _resolve_one_connection(config, "nope")


def test_resolve_one_connection_ambiguous_without_flag_raises() -> None:
    from canonic.cli.commands.ingest import _resolve_one_connection

    config = CanonicConfig(
        version=1,
        project=ProjectConfig(name="t"),
        connections=[
            Connection(id="a", type="sqlite", params={}),
            Connection(id="b", type="sqlite", params={}),
        ],
    )
    with pytest.raises(CanonicConnectionError):
        _resolve_one_connection(config, None)


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
# E11 — recurring wrong_definition outcomes flow into the same ingest run (SPEC-E11 §4)
# ---------------------------------------------------------------------------


_OUTCOME_BINDING = "fct_orders.row_count"  # matches _FakeConnector's only inferred measure


def _seed_binding_and_outcomes(project: Path, *, count: int) -> None:
    """A human_curated ``revenue`` binding plus ``count`` recent wrong_definition outcomes on it.

    References the ``fct_orders``/``row_count`` binding the connector's own evidence produces
    in this same ingest run, so ``validate_contracts`` resolves it (SPEC-E11 §4).
    """
    from datetime import UTC, datetime, timedelta

    from canonic.contracts.loader import dump_metric_binding
    from canonic.contracts.models import CanonicalRef, MetricBinding
    from canonic.instrumentation.events import DiskAnswerEventLog
    from canonic.instrumentation.models import AnswerEvent, AnswerOutcomeEvent

    binding_path = project / "contracts" / "metrics" / "revenue.yaml"
    binding_path.parent.mkdir(parents=True, exist_ok=True)
    binding_path.write_text(
        dump_metric_binding(
            MetricBinding(
                metric="revenue",
                canonical=CanonicalRef(source="fct_orders", measure="row_count"),
            )
        )
    )

    ts = (datetime.now(UTC) - timedelta(days=1)).isoformat()
    log = DiskAnswerEventLog(project)
    for i in range(count):
        log.append(
            AnswerEvent(
                ts=ts,
                contract_schema="2.2",
                query_hash=f"sha256:q{i}",
                compiled_sql_hash="sha256:sql",
                connection="warehouse_pg",
                resolved={"metrics": {"revenue": _OUTCOME_BINDING}},
                latency_ms=10,
            )
        )
        log.append(
            AnswerOutcomeEvent(
                ts=ts,
                ref=f"sha256:q{i}",
                verdict="incorrect",
                reason_code="wrong_definition",
                marked_by="analyst",
            )
        )


def test_recurring_wrong_definition_flags_contradiction(project: Path) -> None:
    """S2-AC2/S3-AC1: a recurring wrong_definition pattern flags the binding for review,
    alongside the connector's own schema evidence, in the same ingest run.
    """
    _seed_binding_and_outcomes(project, count=2)

    result = CliRunner().invoke(app, ["--json", "ingest", "--dry-run"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    notes = payload["report"]["entries"]
    contradiction_targets = {e["target"] for e in notes if e["decision"] == "contradiction"}
    assert "contracts/metrics/revenue.yaml" in contradiction_targets


def test_single_wrong_definition_does_not_flag(project: Path) -> None:
    """S2-AC1: a single incident opens a review flag at most, not a contradiction."""
    _seed_binding_and_outcomes(project, count=1)

    result = CliRunner().invoke(app, ["--json", "ingest", "--dry-run"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    contradiction_targets = {
        e["target"] for e in payload["report"]["entries"] if e["decision"] == "contradiction"
    }
    assert "contracts/metrics/revenue.yaml" not in contradiction_targets


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
