"""Tests for canonic/ingestion/emitter.py (GH-35) — SPEC-E4 §6 diff emission & audit trail."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from canonic.connectors.base import AcquisitionTier, ColumnInfo, RelationSchema, compute_fingerprint
from canonic.ingestion.builder import ContextBuilder
from canonic.ingestion.emitter import (
    AuditTrailWriter,
    DiffEmitter,
    DiffFormat,
    DiskEventLog,
    DiskSnapshotStore,
)
from canonic.ingestion.models import (
    EvidenceItem,
    Proposal,
    ProposalOp,
    ReconciliationDecision,
    ReconciliationEntry,
    ReconciliationReport,
)
from canonic.ingestion.reconciliation import (
    InMemoryAcceptedStore,
    ReconciliationEngine,
)
from canonic.semantic.models import Provenance

if TYPE_CHECKING:
    from pathlib import Path

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_TARGET = "semantics/warehouse_pg/orders.yaml"


def _proposal(
    *,
    target: str = _TARGET,
    op: ProposalOp = ProposalOp.ADD,
    fingerprint: str = "sha256:new",
    provenance: Provenance = Provenance.INFERRED,
    confidence: float = 1.0,
    content: dict[str, Any] | None = None,
) -> Proposal:
    base: dict[str, Any] = content or {"name": "orders", "grain": ["order_id"]}
    return Proposal(
        target=target,
        op=op,
        content=base,
        provenance=provenance,
        confidence=confidence,
        anchored_to=[fingerprint],
    )


def _entry(
    decision: ReconciliationDecision,
    *,
    proposal: Proposal | None = None,
    existing: dict[str, Any] | None = None,
    existing_provenance: Provenance | None = None,
    existing_frozen: bool = False,
    recommended_action: str | None = None,
    auto_apply: bool = False,
) -> ReconciliationEntry:
    prop = proposal or _proposal()
    return ReconciliationEntry(
        decision=decision,
        target=prop.target,
        proposal=prop,
        existing=existing,
        existing_provenance=existing_provenance,
        existing_frozen=existing_frozen,
        recommended_action=recommended_action,
        auto_apply=auto_apply,
    )


def _report(*entries: ReconciliationEntry) -> ReconciliationReport:
    return ReconciliationReport(entries=list(entries))


def _evidence(*, source: str = "warehouse_pg", fingerprint: str = "sha256:new") -> EvidenceItem:
    return EvidenceItem(
        source=source,
        kind="relation_schema",
        acquisition_tier=AcquisitionTier.LIVE,
        payload={"relation": "orders"},
        source_fingerprint=fingerprint,
        observed_at="2026-06-15T12:00:00Z",  # type: ignore[arg-type]
    )


# ---------------------------------------------------------------------------
# Diff generation per op (SPEC-E4 §6)
# ---------------------------------------------------------------------------


class TestDiffGeneration:
    def test_add_has_after_only(self) -> None:
        result = DiffEmitter().emit(_report(_entry(ReconciliationDecision.ADD)))
        assert len(result.diffs) == 1
        diff = result.diffs[0]
        assert diff.op is ProposalOp.ADD
        assert diff.before is None
        assert diff.after is not None
        assert "+name: orders" in diff.patch

    def test_edit_diffs_before_to_after(self) -> None:
        proposal = _proposal(content={"name": "orders", "grain": ["order_id", "line_id"]})
        entry = _entry(
            ReconciliationDecision.EDIT,
            proposal=proposal,
            existing={"name": "orders", "grain": ["order_id"]},
        )
        diff = DiffEmitter().emit(_report(entry)).diffs[0]
        assert diff.op is ProposalOp.EDIT
        assert diff.before is not None and diff.after is not None
        assert "line_id" in diff.patch
        assert diff.patch.startswith("--- a/")

    def test_prune_has_before_only(self) -> None:
        proposal = _proposal(op=ProposalOp.PRUNE, content={})
        entry = _entry(
            ReconciliationDecision.PRUNE,
            proposal=proposal,
            existing={"name": "orders", "grain": ["order_id"]},
        )
        diff = DiffEmitter().emit(_report(entry)).diffs[0]
        assert diff.op is ProposalOp.PRUNE
        assert diff.before is not None
        assert diff.after is None
        assert "-name: orders" in diff.patch

    def test_no_op_emits_no_diff(self) -> None:
        result = DiffEmitter().emit(_report(_entry(ReconciliationDecision.NO_OP)))
        assert result.diffs == []
        assert result.notes == []


# ---------------------------------------------------------------------------
# Format derivation (SPEC-E4 §6)
# ---------------------------------------------------------------------------


class TestFormatDerivation:
    def test_semantics_yaml_is_yaml(self) -> None:
        diff = DiffEmitter().emit(_report(_entry(ReconciliationDecision.ADD))).diffs[0]
        assert diff.format is DiffFormat.YAML

    def test_knowledge_md_is_markdown(self) -> None:
        proposal = _proposal(
            target="knowledge/orders.md", content={"body": "# Orders\n\nThe orders table."}
        )
        diff = DiffEmitter().emit(_report(_entry(ReconciliationDecision.ADD, proposal=proposal)))
        diff0 = diff.diffs[0]
        assert diff0.format is DiffFormat.MARKDOWN
        assert diff0.after == "# Orders\n\nThe orders table."


# ---------------------------------------------------------------------------
# Evidence anchoring (S7-AC1)
# ---------------------------------------------------------------------------


class TestAnchoring:
    def test_diff_carries_proposal_anchors(self) -> None:
        proposal = _proposal(fingerprint="sha256:abc")
        diff = DiffEmitter().emit(_report(_entry(ReconciliationDecision.ADD, proposal=proposal)))
        assert diff.diffs[0].anchored_to == ["sha256:abc"]

    def test_anchors_resolve_to_snapshot(self, tmp_path: Path) -> None:
        """S7-AC1 — every emitted diff anchor resolves to an item in the written snapshot."""
        proposal = _proposal(fingerprint="sha256:abc")
        report = _report(_entry(ReconciliationDecision.ADD, proposal=proposal))
        result = DiffEmitter().emit(report)

        DiskSnapshotStore(tmp_path).write([_evidence(fingerprint="sha256:abc")])
        snapshot = (tmp_path / "raw-sources" / "warehouse_pg" / "evidence.jsonl").read_text()
        fingerprints = {json.loads(line)["source_fingerprint"] for line in snapshot.splitlines()}

        for diff in result.diffs:
            for anchor in diff.anchored_to:
                assert anchor in fingerprints


# ---------------------------------------------------------------------------
# Contradictions surface as notes, never failures (SPEC-E4 §5.4 / S2–S4)
# ---------------------------------------------------------------------------


class TestContradictionNotes:
    def test_contradiction_becomes_note_not_diff(self) -> None:
        entry = _entry(
            ReconciliationDecision.CONTRADICTION,
            existing={"name": "orders"},
            existing_provenance=Provenance.HUMAN_CURATED,
            recommended_action="resolve manually",
        )
        result = DiffEmitter().emit(_report(entry))
        assert result.diffs == []
        assert len(result.notes) == 1
        note = result.notes[0]
        assert note.existing_provenance is Provenance.HUMAN_CURATED
        assert note.incoming_provenance is Provenance.INFERRED
        assert note.recommended_action == "resolve manually"

    def test_frozen_contradiction_marked(self) -> None:
        entry = _entry(
            ReconciliationDecision.CONTRADICTION,
            existing={"name": "orders"},
            existing_provenance=Provenance.INFERRED,
            existing_frozen=True,
        )
        note = DiffEmitter().emit(_report(entry)).notes[0]
        assert note.existing_frozen is True

    def test_render_contradictions_is_the_pr_review_block(self) -> None:
        """render_contradictions() yields the standalone notes block for the auto-PR comment (§5.4)."""
        entry = _entry(
            ReconciliationDecision.CONTRADICTION,
            existing={"name": "orders"},
            existing_provenance=Provenance.HUMAN_CURATED,
            recommended_action="resolve manually",
        )
        result = DiffEmitter().emit(_report(entry))
        block = result.render_contradictions()
        assert block.startswith("## Contradictions (1)")
        assert "resolve manually" in block
        # The same block is embedded in the full markdown body.
        assert block.strip() in result.render_markdown()

    def test_contradiction_note_carries_incoming_tier(self) -> None:
        """SPEC-E3 §7, S6 — ContradictionNote exposes the incoming acquisition tier."""
        proposal = Proposal(
            target=_TARGET,
            op=ProposalOp.ADD,
            content={"name": "orders"},
            provenance=Provenance.INFERRED,
            confidence=1.0,
            acquisition_tier=AcquisitionTier.MODELING,
        )
        entry = _entry(
            ReconciliationDecision.CONTRADICTION,
            proposal=proposal,
            existing={"name": "orders"},
            existing_provenance=Provenance.HUMAN_CURATED,
        )
        note = DiffEmitter().emit(_report(entry)).notes[0]
        assert note.incoming_tier is AcquisitionTier.MODELING

    def test_contradiction_markdown_includes_tier(self) -> None:
        """Rendered contradiction block shows the incoming tier so both sides are visible."""
        proposal = Proposal(
            target=_TARGET,
            op=ProposalOp.ADD,
            content={"name": "orders"},
            provenance=Provenance.INFERRED,
            confidence=1.0,
            acquisition_tier=AcquisitionTier.MODELING,
        )
        entry = _entry(
            ReconciliationDecision.CONTRADICTION,
            proposal=proposal,
            existing={"name": "orders"},
            existing_provenance=Provenance.INFERRED,
        )
        block = DiffEmitter().emit(_report(entry)).render_contradictions()
        assert "modeling" in block


# ---------------------------------------------------------------------------
# Scan snapshot (S7-AC1)
# ---------------------------------------------------------------------------


class TestSnapshot:
    def test_snapshot_written_per_connection(self, tmp_path: Path) -> None:
        DiskSnapshotStore(tmp_path).write(
            [_evidence(source="warehouse_pg"), _evidence(source="crm_pg")]
        )
        assert (tmp_path / "raw-sources" / "warehouse_pg" / "evidence.jsonl").exists()
        assert (tmp_path / "raw-sources" / "crm_pg" / "evidence.jsonl").exists()

    def test_snapshot_is_deterministically_sorted(self, tmp_path: Path) -> None:
        items = [_evidence(fingerprint="sha256:b"), _evidence(fingerprint="sha256:a")]
        DiskSnapshotStore(tmp_path).write(items)
        snapshot = (tmp_path / "raw-sources" / "warehouse_pg" / "evidence.jsonl").read_text()
        fps = [json.loads(line)["source_fingerprint"] for line in snapshot.splitlines()]
        assert fps == ["sha256:a", "sha256:b"]


# ---------------------------------------------------------------------------
# Event log (S7-AC2)
# ---------------------------------------------------------------------------


class TestEventLog:
    def test_one_event_per_decision_with_inputs(self, tmp_path: Path) -> None:
        """S7-AC2 — each decision is logged with tier, confidence, and anchored evidence."""
        report = _report(
            _entry(ReconciliationDecision.ADD, proposal=_proposal(fingerprint="sha256:abc")),
            _entry(ReconciliationDecision.NO_OP),
        )
        DiskEventLog(tmp_path).append(report.entries, run_id="20260626T143201Z")
        log = (tmp_path / ".canonic" / "events.jsonl").read_text()
        events = [json.loads(line) for line in log.splitlines()]
        assert len(events) == 2
        add_event = events[0]
        assert add_event["kind"] == "reconcile_decision"
        assert add_event["ts"]
        assert add_event["run_id"] == "20260626T143201Z"
        assert add_event["decision"] == "add"
        assert add_event["provenance"] == "inferred"
        assert add_event["confidence"] == 1.0
        assert add_event["anchored_to"] == ["sha256:abc"]

    def test_append_accumulates_across_runs(self, tmp_path: Path) -> None:
        log = DiskEventLog(tmp_path)
        log.append(_report(_entry(ReconciliationDecision.ADD)).entries, run_id="run-1")
        log.append(_report(_entry(ReconciliationDecision.EDIT)).entries, run_id="run-2")
        text = (tmp_path / ".canonic" / "events.jsonl").read_text()
        assert len(text.splitlines()) == 2


# ---------------------------------------------------------------------------
# Serialization (SPEC-E4 §6)
# ---------------------------------------------------------------------------


class TestSerialization:
    def test_to_json_is_valid_with_summary(self) -> None:
        result = DiffEmitter().emit(_report(_entry(ReconciliationDecision.ADD)))
        payload = json.loads(result.to_json())
        assert payload["report"]["summary"]["add"] == 1
        assert len(payload["diffs"]) == 1

    def test_render_markdown_has_sections(self) -> None:
        entry = _entry(
            ReconciliationDecision.CONTRADICTION,
            existing={"name": "orders"},
            existing_provenance=Provenance.HUMAN_CURATED,
            recommended_action="resolve manually",
        )
        md = (
            DiffEmitter().emit(_report(_entry(ReconciliationDecision.ADD), entry)).render_markdown()
        )
        assert "## Decisions" in md
        assert "## Diffs (1)" in md
        assert "## Contradictions (1)" in md
        assert "resolve manually" in md


# ---------------------------------------------------------------------------
# Determinism (S9-AC1)
# ---------------------------------------------------------------------------


class TestDeterminism:
    def test_identical_reports_yield_identical_results(self) -> None:
        report = _report(_entry(ReconciliationDecision.ADD), _entry(ReconciliationDecision.NO_OP))
        assert DiffEmitter().emit(report) == DiffEmitter().emit(report)

    def test_snapshots_are_byte_identical(self, tmp_path: Path) -> None:
        a, b = tmp_path / "a", tmp_path / "b"
        items = [_evidence(fingerprint="sha256:a"), _evidence(fingerprint="sha256:b")]
        DiskSnapshotStore(a).write(items)
        DiskSnapshotStore(b).write(items)
        first = (a / "raw-sources" / "warehouse_pg" / "evidence.jsonl").read_bytes()
        second = (b / "raw-sources" / "warehouse_pg" / "evidence.jsonl").read_bytes()
        assert first == second


# ---------------------------------------------------------------------------
# End-to-end: builder → engine → emitter → audit trail
# ---------------------------------------------------------------------------


class TestPipelineIntegration:
    async def test_build_reconcile_emit_writes_audit_trail(self, tmp_path: Path) -> None:
        cols = [
            ColumnInfo(name="order_id", type="int", nullable=False, position=1),
            ColumnInfo(name="amount", type="decimal", nullable=True, position=2),
        ]
        schema = RelationSchema(
            connection="warehouse_pg",
            relation="analytics.fct_orders",
            kind="table",
            columns=cols,
            primary_key=["order_id"],
            foreign_keys=[],
            acquisition_tier=AcquisitionTier.LIVE,
            source_fingerprint=compute_fingerprint(cols, ["order_id"], []),
        )
        fingerprint = schema.source_fingerprint or "sha256:none"
        evidence = EvidenceItem(
            source="warehouse_pg",
            kind="relation_schema",
            acquisition_tier=AcquisitionTier.LIVE,
            payload=schema.model_dump(mode="json"),
            source_fingerprint=fingerprint,
            observed_at="2026-06-15T12:00:00Z",  # type: ignore[arg-type]
        )

        proposals = (await ContextBuilder().build([evidence])).proposals
        report = ReconciliationEngine().reconcile(proposals, InMemoryAcceptedStore())
        result = DiffEmitter().emit(report)
        AuditTrailWriter.for_project(tmp_path).write([evidence], report, run_id="20260626T000000Z")

        # One add diff, anchored to the evidence fingerprint.
        assert [d.op for d in result.diffs] == [ProposalOp.ADD]
        assert result.diffs[0].anchored_to == [fingerprint]

        # Snapshot resolves the anchor; event log records the decision (S7-AC1/AC2).
        snapshot = (tmp_path / "raw-sources" / "warehouse_pg" / "evidence.jsonl").read_text()
        snap_fps = {json.loads(line)["source_fingerprint"] for line in snapshot.splitlines()}
        assert fingerprint in snap_fps

        events = (tmp_path / ".canonic" / "events.jsonl").read_text().splitlines()
        ev = json.loads(events[0])
        assert ev["kind"] == "reconcile_decision"
        assert ev["decision"] == "add"
