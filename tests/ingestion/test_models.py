"""Tests for canonic/ingestion/models.py (GH-32)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from canonic.connectors.base import AcquisitionTier
from canonic.ingestion.models import (
    KNOWN_EVIDENCE_KINDS,
    DraftedBy,
    EvidenceItem,
    EvidenceKind,
    Proposal,
    ProposalOp,
    ReconciliationDecision,
    ReconciliationEntry,
    ReconciliationReport,
)
from canonic.semantic.models import Provenance

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_NOW = datetime(2026, 6, 15, 12, 0, 0, tzinfo=UTC)


def _evidence(kind: str = "relation_schema") -> EvidenceItem:
    return EvidenceItem(
        source="warehouse_pg",
        kind=kind,
        acquisition_tier=AcquisitionTier.LIVE,
        payload={"relation": "analytics.fct_orders"},
        source_fingerprint="sha256:abc123",
        observed_at=_NOW,
    )


def _proposal(
    op: ProposalOp = ProposalOp.ADD,
    confidence: float = 0.82,
    provenance: Provenance = Provenance.INFERRED,
    drafted_by: DraftedBy = DraftedBy.DETERMINISTIC,
) -> Proposal:
    return Proposal(
        target="semantics/warehouse_pg/orders.yaml",
        op=op,
        content={"name": "orders"},
        provenance=provenance,
        confidence=confidence,
        anchored_to=["sha256:abc123"],
        drafted_by=drafted_by,
    )


def _entry(decision: ReconciliationDecision, **kwargs: object) -> ReconciliationEntry:
    return ReconciliationEntry(
        decision=decision,
        target="semantics/warehouse_pg/orders.yaml",
        proposal=_proposal(),
        **kwargs,  # type: ignore[arg-type]
    )


# ---------------------------------------------------------------------------
# EvidenceItem
# ---------------------------------------------------------------------------


class TestEvidenceItem:
    def test_unknown_kind_accepted(self) -> None:
        """AC1 — open set: unknown kinds must not raise."""
        item = _evidence(kind="answer_outcome")
        assert item.kind == "answer_outcome"
        assert item.is_known() is False

    def test_known_kind_recognised(self) -> None:
        item = _evidence(kind="relation_schema")
        assert item.is_known() is True

    def test_all_evidence_kinds_are_known(self) -> None:
        for kind in EvidenceKind:
            assert kind in KNOWN_EVIDENCE_KINDS

    def test_known_evidence_kinds_constant(self) -> None:
        assert {
            "relation_schema",
            "observed_query",
            "definition",
            "doc_evidence",
            "usage_evidence",
        } == KNOWN_EVIDENCE_KINDS


# ---------------------------------------------------------------------------
# EvidenceKind enum
# ---------------------------------------------------------------------------


class TestEvidenceKind:
    def test_values_are_lowercase(self) -> None:
        assert EvidenceKind.RELATION_SCHEMA == "relation_schema"
        assert EvidenceKind.OBSERVED_QUERY == "observed_query"
        assert EvidenceKind.DEFINITION == "definition"
        assert EvidenceKind.DOC_EVIDENCE == "doc_evidence"


# ---------------------------------------------------------------------------
# Proposal
# ---------------------------------------------------------------------------


class TestProposal:
    def test_round_trip(self) -> None:
        """AC2 — serialization round-trip."""
        p = _proposal(confidence=0.82)
        assert Proposal.model_validate(p.model_dump()) == p

    def test_default_provenance_is_inferred(self) -> None:
        """New evidence always enters at the lowest tier (spec §4)."""
        p = Proposal(
            target="semantics/w/orders.yaml",
            op=ProposalOp.ADD,
            content={},
            confidence=1.0,
        )
        assert p.provenance is Provenance.INFERRED

    def test_default_drafted_by_is_deterministic(self) -> None:
        p = Proposal(target="t", op=ProposalOp.ADD, content={}, confidence=1.0)
        assert p.drafted_by is DraftedBy.DETERMINISTIC

    def test_confidence_upper_bound(self) -> None:
        with pytest.raises(ValidationError):
            _proposal(confidence=1.5)

    def test_confidence_lower_bound(self) -> None:
        with pytest.raises(ValidationError):
            _proposal(confidence=-0.1)

    def test_confidence_boundaries_valid(self) -> None:
        _proposal(confidence=0.0)
        _proposal(confidence=1.0)

    def test_anchored_to_defaults_empty(self) -> None:
        p = Proposal(target="t", op=ProposalOp.EDIT, content={}, confidence=0.5)
        assert p.anchored_to == []


# ---------------------------------------------------------------------------
# ReconciliationDecision enum
# ---------------------------------------------------------------------------


class TestReconciliationDecision:
    def test_values_are_lowercase(self) -> None:
        assert ReconciliationDecision.NO_OP == "no_op"
        assert ReconciliationDecision.CONTRADICTION == "contradiction"
        assert ReconciliationDecision.ADD == "add"
        assert ReconciliationDecision.EDIT == "edit"
        assert ReconciliationDecision.PRUNE == "prune"


# ---------------------------------------------------------------------------
# ReconciliationEntry
# ---------------------------------------------------------------------------


class TestReconciliationEntry:
    def test_contradiction_entry_carries_both_sides(self) -> None:
        entry = _entry(
            ReconciliationDecision.CONTRADICTION,
            existing={"grain": ["order_id"]},
            existing_provenance=Provenance.HUMAN_CURATED,
            recommended_action="Keep existing human_curated grain; discard inferred proposal.",
        )
        assert entry.existing is not None
        assert entry.existing_provenance is Provenance.HUMAN_CURATED
        assert entry.recommended_action is not None

    def test_add_entry_has_no_existing(self) -> None:
        entry = _entry(ReconciliationDecision.ADD)
        assert entry.existing is None
        assert entry.existing_provenance is None


# ---------------------------------------------------------------------------
# ReconciliationReport
# ---------------------------------------------------------------------------


class TestReconciliationReport:
    def test_summary_counts(self) -> None:
        """AC3 — summary aggregates entries by decision."""
        report = ReconciliationReport(
            entries=[
                _entry(ReconciliationDecision.ADD),
                _entry(ReconciliationDecision.ADD),
                _entry(ReconciliationDecision.EDIT),
                _entry(ReconciliationDecision.NO_OP),
                _entry(ReconciliationDecision.CONTRADICTION),
            ]
        )
        assert report.summary == {
            "add": 2,
            "edit": 1,
            "prune": 0,
            "no_op": 1,
            "contradiction": 1,
        }

    def test_summary_in_model_dump(self) -> None:
        """summary is a computed_field so it must appear in serialized output."""
        report = ReconciliationReport(entries=[_entry(ReconciliationDecision.ADD)])
        dumped = report.model_dump()
        assert "summary" in dumped
        assert dumped["summary"]["add"] == 1

    def test_empty_report_all_zero(self) -> None:
        report = ReconciliationReport()
        assert all(v == 0 for v in report.summary.values())

    def test_entries_default_empty(self) -> None:
        assert ReconciliationReport().entries == []
