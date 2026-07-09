"""E11 outcome-evidence → builder → reconciliation → emission (SPEC-E11 §4, S2, S3)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from canonic.ingestion.builder import ContextBuilder
from canonic.ingestion.emitter import DiffEmitter
from canonic.ingestion.models import EvidenceItem, EvidenceKind, ReconciliationDecision
from canonic.ingestion.reconciliation import (
    ExistingFact,
    InMemoryAcceptedStore,
    ReconciliationEngine,
)
from canonic.semantic.models import Provenance

_TARGET = "contracts/metrics/revenue.yaml"
_SENTINEL = "_answer_outcome_evidence"


def _outcome_evidence(**payload_overrides: Any) -> EvidenceItem:
    payload: dict[str, Any] = {
        "metric": "revenue",
        "binding": "orders.total_revenue",
        "count": 2,
        "window_days": 90,
        "distinct_markers": 2,
        "refs": ["sha256:1", "sha256:2"],
        **payload_overrides,
    }
    return EvidenceItem(
        source="canonic.feedback",
        kind=EvidenceKind.ANSWER_OUTCOME.value,
        acquisition_tier="query_history",
        payload=payload,
        source_fingerprint="sha256:feedback:revenue",
        observed_at=datetime.now(UTC),
    )


def _existing_binding(
    *, provenance: Provenance = Provenance.HUMAN_CURATED, frozen: bool = False
) -> ExistingFact:
    return ExistingFact(
        target=_TARGET,
        content={
            "metric": "revenue",
            "canonical": {"source": "orders", "measure": "total_revenue"},
            "provenance": provenance.value,
            "status": "active",
        },
        provenance=provenance,
        frozen=frozen,
    )


class TestBuilderDispatch:
    async def test_builds_inferred_proposal_on_metric_target(self) -> None:
        result = await ContextBuilder().build([_outcome_evidence()])
        assert len(result.proposals) == 1
        proposal = result.proposals[0]
        assert proposal.target == _TARGET
        assert proposal.provenance is Provenance.INFERRED
        assert _SENTINEL in proposal.content
        assert proposal.anchored_to == ["sha256:1", "sha256:2"]

    async def test_slugifies_metric_name_for_target(self) -> None:
        result = await ContextBuilder().build([_outcome_evidence(metric="Net Revenue!")])
        assert result.proposals[0].target == "contracts/metrics/net_revenue.yaml"


class TestReconciliationAlwaysContradicts:
    """S2-AC2/S3: outcome evidence only ever flags, never edits or adds (S3-AC2)."""

    async def test_human_curated_binding_flagged_not_overwritten(self) -> None:
        """S3-AC1: against a human_curated binding, flagged for review, no overwrite."""
        proposals = (await ContextBuilder().build([_outcome_evidence()])).proposals
        existing = _existing_binding(provenance=Provenance.HUMAN_CURATED)
        report = ReconciliationEngine().reconcile(proposals, InMemoryAcceptedStore([existing]))

        assert len(report.entries) == 1
        entry = report.entries[0]
        assert entry.decision is ReconciliationDecision.CONTRADICTION
        assert entry.existing == existing.content

    async def test_inferred_binding_still_contradicts_never_edits(self) -> None:
        """Even against an inferred (lower-tier) existing fact, E11 never proposes an EDIT —
        it has no corrected definition to offer, only a pattern of wrong marks (§4).
        """
        proposals = (await ContextBuilder().build([_outcome_evidence()])).proposals
        existing = _existing_binding(provenance=Provenance.INFERRED)
        report = ReconciliationEngine().reconcile(proposals, InMemoryAcceptedStore([existing]))

        assert report.entries[0].decision is ReconciliationDecision.CONTRADICTION

    async def test_frozen_binding_flagged(self) -> None:
        proposals = (await ContextBuilder().build([_outcome_evidence()])).proposals
        existing = _existing_binding(provenance=Provenance.HUMAN_CURATED, frozen=True)
        report = ReconciliationEngine().reconcile(proposals, InMemoryAcceptedStore([existing]))

        entry = report.entries[0]
        assert entry.decision is ReconciliationDecision.CONTRADICTION
        assert entry.existing_frozen is True

    async def test_missing_binding_flagged_not_added(self) -> None:
        """The target binding no longer exists — E11 never fabricates one (never ADD)."""
        proposals = (await ContextBuilder().build([_outcome_evidence()])).proposals
        report = ReconciliationEngine().reconcile(proposals, InMemoryAcceptedStore([]))

        entry = report.entries[0]
        assert entry.decision is ReconciliationDecision.CONTRADICTION
        assert entry.existing is None

    async def test_no_file_is_ever_written(self) -> None:
        """S3-AC2: no committed file is edited in place; every change is a reviewable diff."""
        proposals = (await ContextBuilder().build([_outcome_evidence()])).proposals
        existing = _existing_binding(provenance=Provenance.INFERRED)
        report = ReconciliationEngine().reconcile(proposals, InMemoryAcceptedStore([existing]))

        emission = DiffEmitter().emit(report)
        assert emission.diffs == []
        assert len(emission.notes) == 1
        assert emission.notes[0].target == _TARGET
