"""Tests for canon/ingestion/pending.py (GH-149) — SPEC-E4 §6, E1 §7."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from canon.ingestion.emitter import DiffEmitter
from canon.ingestion.models import (
    Proposal,
    ProposalOp,
    ReconciliationDecision,
    ReconciliationEntry,
    ReconciliationReport,
)
from canon.ingestion.pending import (
    PendingDiffStore,
    PendingStatus,
    ProposalStatus,
    generate_run_id,
)
from canon.semantic.models import Provenance

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


def _emission_with_diffs(n: int = 2) -> Any:
    """Return an EmissionResult with ``n`` ADD diffs."""
    entries = [
        _entry(ReconciliationDecision.ADD, target=f"semantics/warehouse_pg/table_{i}.yaml")
        for i in range(1, n + 1)
    ]
    return DiffEmitter().emit(_report(*entries))


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

        assert run_dir == tmp_path / ".canon" / "pending-diffs" / run_id
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

        run_dir = tmp_path / ".canon" / "pending-diffs" / run_id
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
        from canon.config import ReconcileConfig, scaffold_project
        from canon.connectors.base import (
            AcquisitionTier,
            Capability,
            ColumnInfo,
            ConnectorBase,
            Health,
            RelationSchema,
            compute_fingerprint,
        )
        from canon.ingestion.pipeline import IngestionPipeline
        from canon.ingestion.source import evidence_from_introspection

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

        pending_root = tmp_path / ".canon" / "pending-diffs"
        assert pending_root.exists(), "pending-diffs root not created"
        run_dirs = list(pending_root.iterdir())
        assert len(run_dirs) == 1, "expected exactly one pending-diff run dir"
        run_id = run_dirs[0].name

        events_path = tmp_path / ".canon" / "events.jsonl"
        assert events_path.exists(), "events.jsonl not written"
        events = [json.loads(line) for line in events_path.read_text().splitlines()]
        reconcile_events = [e for e in events if e["kind"] == "reconcile_decision"]
        assert reconcile_events, "no reconcile_decision events found"
        for ev in reconcile_events:
            assert ev["run_id"] == run_id, (
                f"event run_id {ev['run_id']!r} != pending dir {run_id!r}"
            )
