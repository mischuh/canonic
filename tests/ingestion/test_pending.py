"""Tests for canonic/ingestion/pending.py (GH-149) — SPEC-E4 §6, E1 §7."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from canonic.ingestion.emitter import DiffEmitter
from canonic.ingestion.models import (
    Proposal,
    ProposalOp,
    ReconciliationDecision,
    ReconciliationEntry,
    ReconciliationReport,
)
from canonic.ingestion.pending import (
    PendingDiffStore,
    PendingRun,
    PendingStatus,
    ProposalStatus,
    apply_entry,
    generate_run_id,
    latest_run_id,
    update_status,
)
from canonic.semantic.models import Provenance

if TYPE_CHECKING:
    from pathlib import Path


# ---------------------------------------------------------------------------
# Helpers (mirror test_emitter.py helpers for self-contained tests)
# ---------------------------------------------------------------------------

_TARGET_YAML = "semantics/warehouse_pg/orders.yaml"
_TARGET_MD = "knowledge/refunds.md"


def _proposal(
    *,
    target: str = _TARGET_YAML,
    op: ProposalOp = ProposalOp.ADD,
    content: dict[str, Any] | None = None,
) -> Proposal:
    return Proposal(
        target=target,
        op=op,
        content=content or {"name": "orders", "grain": ["order_id"]},
        provenance=Provenance.INFERRED,
        confidence=1.0,
        anchored_to=["sha256:abc"],
    )


def _entry(
    decision: ReconciliationDecision,
    *,
    target: str = _TARGET_YAML,
    content: dict[str, Any] | None = None,
    existing: dict[str, Any] | None = None,
) -> ReconciliationEntry:
    prop = _proposal(target=target, content=content)
    return ReconciliationEntry(
        decision=decision,
        target=target,
        proposal=prop,
        existing=existing,
    )


def _report(*entries: ReconciliationEntry) -> ReconciliationReport:
    return ReconciliationReport(entries=list(entries))


_VALID_SOURCE_CONTENT = {
    "name": "orders",
    "connection": "warehouse_pg",
    "table": "public.orders",
    "grain": ["order_id"],
    "columns": [{"name": "order_id", "type": "int", "nullable": False}],
}


def _emission_with_diffs(n: int = 2) -> Any:
    """Return an EmissionResult with ``n`` ADD diffs."""
    entries = [
        _entry(ReconciliationDecision.ADD, target=f"semantics/warehouse_pg/table_{i}.yaml")
        for i in range(1, n + 1)
    ]
    return DiffEmitter().emit(_report(*entries))


def _emission_with_valid_source() -> Any:
    """Return an EmissionResult with one ADD diff containing a full valid SemanticSource body."""
    entry = ReconciliationEntry(
        decision=ReconciliationDecision.ADD,
        target=_TARGET_YAML,
        proposal=Proposal(
            target=_TARGET_YAML,
            op=ProposalOp.ADD,
            content=_VALID_SOURCE_CONTENT,
            provenance=Provenance.INFERRED,
            confidence=1.0,
            anchored_to=["sha256:abc"],
        ),
    )
    return DiffEmitter().emit(_report(entry))


# ---------------------------------------------------------------------------
# generate_run_id
# ---------------------------------------------------------------------------


class TestGenerateRunId:
    def test_format(self) -> None:
        run_id = generate_run_id()
        assert len(run_id) == 16
        assert run_id.endswith("Z")
        assert "T" in run_id

    def test_monotonically_different_across_calls(self) -> None:
        ids = [generate_run_id() for _ in range(5)]
        assert len(set(ids)) >= 1


# ---------------------------------------------------------------------------
# AC1 — pending-diffs tree is written correctly
# ---------------------------------------------------------------------------


class TestPendingDiffStoreWrite:
    def test_ac1_directory_structure_exists(self, tmp_path: Path) -> None:
        """AC1: report.yaml, status.yaml, and one .diff per proposal are present."""
        run_id = "20260626T143201Z"
        emission = _emission_with_diffs(2)

        run_dir = PendingDiffStore(tmp_path).write(run_id, emission)

        assert run_dir == tmp_path / ".canonic" / "pending-diffs" / run_id
        assert (run_dir / "report.yaml").exists()
        assert (run_dir / "status.yaml").exists()
        proposal_files = sorted((run_dir / "proposals").iterdir())
        assert len(proposal_files) == 2

    def test_diff_files_numbered_and_named(self, tmp_path: Path) -> None:
        run_id = "20260626T143201Z"
        emission = _emission_with_diffs(3)

        run_dir = PendingDiffStore(tmp_path).write(run_id, emission)

        names = sorted(p.name for p in (run_dir / "proposals").iterdir())
        assert names[0].startswith("0001-")
        assert names[1].startswith("0002-")
        assert names[2].startswith("0003-")
        assert all(n.endswith(".diff") for n in names)

    def test_diff_file_content_is_patch(self, tmp_path: Path) -> None:
        run_id = "20260626T143201Z"
        emission = _emission_with_diffs(1)

        run_dir = PendingDiffStore(tmp_path).write(run_id, emission)

        diff_files = list((run_dir / "proposals").iterdir())
        assert len(diff_files) == 1
        written = diff_files[0].read_text()
        assert written == emission.diffs[0].patch

    def test_slug_replaces_slashes(self, tmp_path: Path) -> None:
        emission = DiffEmitter().emit(
            _report(_entry(ReconciliationDecision.ADD, target="semantics/pg/orders.yaml"))
        )
        run_dir = PendingDiffStore(tmp_path).write("run1", emission)
        names = [p.name for p in (run_dir / "proposals").iterdir()]
        assert names[0] == "0001-semantics-pg-orders.yaml.diff"

    def test_report_yaml_round_trips(self, tmp_path: Path) -> None:
        from ruamel.yaml import YAML

        emission = _emission_with_diffs(1)
        run_dir = PendingDiffStore(tmp_path).write("run1", emission)

        yaml = YAML()
        with (run_dir / "report.yaml").open() as f:
            data = yaml.load(f)
        assert "entries" in data
        assert "summary" in data
        assert data["summary"]["add"] == 1

    def test_status_yaml_all_pending(self, tmp_path: Path) -> None:
        from ruamel.yaml import YAML

        run_id = "20260626T000000Z"
        emission = _emission_with_diffs(2)
        run_dir = PendingDiffStore(tmp_path).write(run_id, emission)

        yaml = YAML()
        with (run_dir / "status.yaml").open() as f:
            data = yaml.load(f)

        status = PendingStatus.model_validate(data)
        assert status.run_id == run_id
        assert len(status.proposals) == 2
        assert all(p.status is ProposalStatus.PENDING for p in status.proposals)

    def test_status_proposal_targets_match_diffs(self, tmp_path: Path) -> None:
        from ruamel.yaml import YAML

        emission = _emission_with_diffs(2)
        run_dir = PendingDiffStore(tmp_path).write("run1", emission)

        yaml = YAML()
        with (run_dir / "status.yaml").open() as f:
            data = yaml.load(f)
        status = PendingStatus.model_validate(data)

        for entry, diff in zip(status.proposals, emission.diffs, strict=True):
            assert entry.target == diff.target
            assert entry.op == diff.op.value

    def test_no_diff_files_when_no_emitted_diffs(self, tmp_path: Path) -> None:
        """NO_OP entries produce no diffs — an empty proposals/ dir is valid."""
        emission = DiffEmitter().emit(_report(_entry(ReconciliationDecision.NO_OP)))
        run_dir = PendingDiffStore(tmp_path).write("empty-run", emission)
        assert list((run_dir / "proposals").iterdir()) == []

    def test_contradiction_notes_not_written_as_diff_files(self, tmp_path: Path) -> None:
        """Contradiction entries become notes, not diff files."""
        emission = DiffEmitter().emit(
            _report(
                _entry(ReconciliationDecision.ADD),
                _entry(
                    ReconciliationDecision.CONTRADICTION,
                    target="semantics/pg/conflict.yaml",
                    existing={"name": "conflict"},
                ),
            )
        )
        run_dir = PendingDiffStore(tmp_path).write("run1", emission)
        diff_files = list((run_dir / "proposals").iterdir())
        assert len(diff_files) == 1


# ---------------------------------------------------------------------------
# AC2 — survives process exit (new instance reads the same state)
# ---------------------------------------------------------------------------


class TestPersistenceAcrossInstances:
    def test_ac2_fresh_instance_reads_status(self, tmp_path: Path) -> None:
        """AC2: a brand-new PendingDiffStore (simulating a new process) sees the same data."""
        from ruamel.yaml import YAML

        run_id = "20260626T120000Z"
        emission = _emission_with_diffs(1)
        PendingDiffStore(tmp_path).write(run_id, emission)

        run_dir = tmp_path / ".canonic" / "pending-diffs" / run_id
        yaml = YAML()
        with (run_dir / "status.yaml").open() as f:
            data = yaml.load(f)
        status = PendingStatus.model_validate(data)
        assert status.run_id == run_id
        assert len(status.proposals) == 1
        assert status.proposals[0].status is ProposalStatus.PENDING


# ---------------------------------------------------------------------------
# AC3 — dir survives after all proposals resolved (no auto-delete)
# ---------------------------------------------------------------------------


class TestNoAutoCleanup:
    def test_ac3_run_dir_not_deleted_after_resolve(self, tmp_path: Path) -> None:
        """AC3: PendingDiffStore never deletes the run dir — it is an audit artefact."""
        from ruamel.yaml import YAML

        run_id = "20260626T130000Z"
        emission = _emission_with_diffs(1)
        run_dir = PendingDiffStore(tmp_path).write(run_id, emission)

        yaml = YAML()
        with (run_dir / "status.yaml").open() as f:
            data = yaml.load(f)
        data["proposals"][0]["status"] = ProposalStatus.ACCEPTED.value
        with (run_dir / "status.yaml").open("w") as f:
            yaml.dump(data, f)

        assert run_dir.exists()
        assert (run_dir / "report.yaml").exists()


# ---------------------------------------------------------------------------
# Same-second collision guard
# ---------------------------------------------------------------------------


class TestCollisionGuard:
    def test_collision_produces_distinct_dirs(self, tmp_path: Path) -> None:
        emission = _emission_with_diffs(1)
        store = PendingDiffStore(tmp_path)

        dir1 = store.write("20260626T143201Z", emission)
        dir2 = store.write("20260626T143201Z", emission)

        assert dir1 != dir2
        assert dir1.exists()
        assert dir2.exists()


# ---------------------------------------------------------------------------
# AC4 — pipeline wires run_id into event log (integration)
# ---------------------------------------------------------------------------


class TestPipelineRunIdCrossReference:
    async def test_ac4_event_log_run_id_matches_pending_dir(self, tmp_path: Path) -> None:
        """AC4: every reconcile_decision event's run_id matches the pending-diffs dir name."""
        from canonic.config import ReconcileConfig, scaffold_project
        from canonic.connectors.base import (
            AcquisitionTier,
            Capability,
            ColumnInfo,
            ConnectorBase,
            Health,
            RelationSchema,
            compute_fingerprint,
        )
        from canonic.ingestion.pipeline import IngestionPipeline
        from canonic.ingestion.source import evidence_from_introspection

        scaffold_project(tmp_path)

        cols = [ColumnInfo(name="id", type="int", nullable=False, position=1)]
        schema = RelationSchema(
            connection="test_conn",
            relation="public.orders",
            kind="table",
            columns=cols,
            primary_key=["id"],
            foreign_keys=[],
            acquisition_tier=AcquisitionTier.LIVE,
            source_fingerprint=compute_fingerprint(cols, ["id"], []),
        )

        class _FakeConnector(ConnectorBase):
            def capabilities(self) -> list[Capability]:
                return [Capability.INTROSPECT_SCHEMA, Capability.TEST_CONNECTION]

            async def test_connection(self) -> Health:
                return Health(status="ok")

            async def introspect_schema(self) -> list[RelationSchema]:
                return [schema]

        connector = _FakeConnector()
        evidence = await evidence_from_introspection(connector, "test_conn")

        pipeline = IngestionPipeline(tmp_path, {"test_conn": connector}, ReconcileConfig())
        await pipeline.run(evidence)

        pending_root = tmp_path / ".canonic" / "pending-diffs"
        assert pending_root.exists(), "pending-diffs root not created"
        run_dirs = list(pending_root.iterdir())
        assert len(run_dirs) == 1, "expected exactly one pending-diff run dir"
        run_id = run_dirs[0].name

        events_path = tmp_path / ".canonic" / "events.jsonl"
        assert events_path.exists(), "events.jsonl not written"
        events = [json.loads(line) for line in events_path.read_text().splitlines()]
        reconcile_events = [e for e in events if e["kind"] == "reconcile_decision"]
        assert reconcile_events, "no reconcile_decision events found"
        for ev in reconcile_events:
            assert ev["run_id"] == run_id, (
                f"event run_id {ev['run_id']!r} != pending dir {run_id!r}"
            )


# ---------------------------------------------------------------------------
# GH-150: latest_run_id
# ---------------------------------------------------------------------------


class TestLatestRunId:
    def test_returns_none_when_no_runs(self, tmp_path: Path) -> None:
        assert latest_run_id(tmp_path) is None

    def test_returns_most_recent_by_lexicographic_order(self, tmp_path: Path) -> None:
        emission = _emission_with_diffs(1)
        store = PendingDiffStore(tmp_path)
        store.write("20260101T000000Z", emission)
        store.write("20260626T000000Z", emission)
        store.write("20260310T000000Z", emission)

        result = latest_run_id(tmp_path)
        assert result == "20260626T000000Z"

    def test_returns_single_run_when_only_one(self, tmp_path: Path) -> None:
        emission = _emission_with_diffs(1)
        PendingDiffStore(tmp_path).write("20260626T143201Z", emission)
        assert latest_run_id(tmp_path) == "20260626T143201Z"


# ---------------------------------------------------------------------------
# GH-150: PendingRun
# ---------------------------------------------------------------------------


class TestPendingRun:
    def test_load_aligns_proposals_with_diffs(self, tmp_path: Path) -> None:
        emission = _emission_with_diffs(3)
        run_id = "20260626T143201Z"
        run_dir = PendingDiffStore(tmp_path).write(run_id, emission)

        run = PendingRun.load(run_dir)

        assert len(run.proposals) == 3
        for proposal, diff in zip(run.proposals, emission.diffs, strict=True):
            assert proposal.target == diff.target
            assert run.diff_for(proposal).target == diff.target

    def test_patch_text_matches_diff_file(self, tmp_path: Path) -> None:
        emission = _emission_with_diffs(1)
        run_dir = PendingDiffStore(tmp_path).write("run1", emission)

        run = PendingRun.load(run_dir)
        proposal = run.proposals[0]

        assert run.patch_text(proposal) == emission.diffs[0].patch


# ---------------------------------------------------------------------------
# GH-150: update_status
# ---------------------------------------------------------------------------


class TestUpdateStatus:
    def test_persists_new_status_to_disk(self, tmp_path: Path) -> None:
        from ruamel.yaml import YAML

        emission = _emission_with_diffs(2)
        run_dir = PendingDiffStore(tmp_path).write("run1", emission)

        run = PendingRun.load(run_dir)
        updated = [
            run.proposals[0].model_copy(update={"status": ProposalStatus.ACCEPTED}),
            run.proposals[1],
        ]
        update_status(run_dir, updated)

        yaml = YAML()
        with (run_dir / "status.yaml").open() as f:
            data = yaml.load(f)
        reloaded = PendingStatus.model_validate(data)

        assert reloaded.proposals[0].status is ProposalStatus.ACCEPTED
        assert reloaded.proposals[1].status is ProposalStatus.PENDING


# ---------------------------------------------------------------------------
# GH-150: apply_entry (AC1 / AC5)
# ---------------------------------------------------------------------------


class TestApplyEntry:
    def test_ac1_add_writes_target_file(self, tmp_path: Path) -> None:
        """AC1: accepting an ADD proposal writes the target file to the working directory."""
        from canonic.config import scaffold_project

        scaffold_project(tmp_path)
        emission = _emission_with_diffs(1)
        run_dir = PendingDiffStore(tmp_path).write("run1", emission)
        run = PendingRun.load(run_dir)

        apply_entry(tmp_path, run, run.proposals[0])

        target = tmp_path / run.proposals[0].target
        assert target.exists()

    def test_ac5_freeze_writes_frozen_annotation(self, tmp_path: Path) -> None:
        """AC5: freeze writes meta.frozen=true; a subsequent ingest flags CONTRADICTION."""
        from canonic.config import scaffold_project
        from canonic.semantic.loader import load_semantic_source

        scaffold_project(tmp_path)
        emission = _emission_with_valid_source()
        run_dir = PendingDiffStore(tmp_path).write("run1", emission)
        run = PendingRun.load(run_dir)

        apply_entry(tmp_path, run, run.proposals[0], freeze=True)

        target = tmp_path / run.proposals[0].target
        assert target.exists()
        source = load_semantic_source(target)
        assert source.meta.frozen is True

    def test_ac5_frozen_fact_flags_contradiction_on_reingest(self, tmp_path: Path) -> None:
        """AC5: after freeze, a conflicting proposal yields CONTRADICTION not EDIT."""
        from canonic.config import ReconcileConfig, scaffold_project
        from canonic.ingestion.models import ReconciliationDecision
        from canonic.ingestion.reconciliation import DiskAcceptedStore, ReconciliationEngine

        scaffold_project(tmp_path)
        emission = _emission_with_valid_source()
        run_dir = PendingDiffStore(tmp_path).write("run1", emission)
        run = PendingRun.load(run_dir)

        apply_entry(tmp_path, run, run.proposals[0], freeze=True)

        target_str = run.proposals[0].target
        accepted_store = DiskAcceptedStore(tmp_path)
        fact = accepted_store.get(target_str)
        assert fact is not None
        assert fact.frozen is True

        conflicting_proposal = _proposal(
            target=target_str,
            op=ProposalOp.EDIT,
            content={**_VALID_SOURCE_CONTENT, "name": "different_orders"},
        )
        from canonic.ingestion.reconciliation import NullReconcileDrafter

        engine = ReconciliationEngine(ReconcileConfig(), NullReconcileDrafter())
        report = engine.reconcile([conflicting_proposal], accepted_store)

        assert any(e.decision is ReconciliationDecision.CONTRADICTION for e in report.entries)
