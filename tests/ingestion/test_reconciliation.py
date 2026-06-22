"""Tests for canon/ingestion/reconciliation.py (GH-34) — SPEC-E4 §5 decision table."""

from __future__ import annotations

from typing import Any

from canon.config import AutoApplyConfig, ReconcileConfig
from canon.connectors.base import AcquisitionTier, ColumnInfo, RelationSchema, compute_fingerprint
from canon.ingestion.builder import ContextBuilder
from canon.ingestion.models import (
    EvidenceItem,
    Proposal,
    ProposalOp,
    ReconciliationDecision,
)
from canon.ingestion.reconciliation import (
    ExistingFact,
    InMemoryAcceptedStore,
    NullReconcileDrafter,
    ReconciliationEngine,
    ResolutionDraft,
)
from canon.semantic.models import Provenance

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
    tier: AcquisitionTier = AcquisitionTier.LIVE,
) -> Proposal:
    base: dict[str, Any] = {
        "name": "orders",
        "grain": ["order_id"],
        "joins": [],
        "measures": [],
        "meta": {"source_fingerprint": fingerprint},
    }
    if content:
        base.update(content)
    return Proposal(
        target=target,
        op=op,
        content=base,
        provenance=provenance,
        confidence=confidence,
        anchored_to=[fingerprint],
        acquisition_tier=tier,
    )


def _existing(
    *,
    target: str = _TARGET,
    provenance: Provenance = Provenance.INFERRED,
    frozen: bool = False,
    fingerprint: str = "sha256:old",
    content: dict[str, Any] | None = None,
) -> ExistingFact:
    return ExistingFact(
        target=target,
        content=content or {"name": "orders", "grain": ["order_id"], "joins": [], "measures": []},
        provenance=provenance,
        frozen=frozen,
        source_fingerprint=fingerprint,
    )


def _store(*facts: ExistingFact) -> InMemoryAcceptedStore:
    return InMemoryAcceptedStore(facts)


# ---------------------------------------------------------------------------
# No existing fact → add (decision-table row 1)
# ---------------------------------------------------------------------------


class TestAdd:
    def test_no_existing_fact_proposes_add(self) -> None:
        report = ReconciliationEngine().reconcile([_proposal()], _store())
        assert len(report.entries) == 1
        assert report.entries[0].decision is ReconciliationDecision.ADD


# ---------------------------------------------------------------------------
# Idempotency — equal fingerprint → no-op; drift → single edit (S6)
# ---------------------------------------------------------------------------


class TestIdempotency:
    def test_equal_fingerprint_is_noop(self) -> None:
        existing = _existing(fingerprint="sha256:same")
        report = ReconciliationEngine().reconcile(
            [_proposal(fingerprint="sha256:same")], _store(existing)
        )
        assert [e.decision for e in report.entries] == [ReconciliationDecision.NO_OP]

    def test_changed_fingerprint_proposes_single_edit(self) -> None:
        existing = _existing(fingerprint="sha256:old")
        report = ReconciliationEngine().reconcile(
            [_proposal(fingerprint="sha256:new")], _store(existing)
        )
        assert [e.decision for e in report.entries] == [ReconciliationDecision.EDIT]


# ---------------------------------------------------------------------------
# Provenance protects curated facts (S2) and freeze (S3)
# ---------------------------------------------------------------------------


class TestProvenanceAndFreeze:
    def test_curated_fact_vs_inferred_proposal_flags_contradiction(self) -> None:
        """S2 — curated fact kept, conflict flagged, no edit proposed to it."""
        existing = _existing(provenance=Provenance.HUMAN_CURATED, fingerprint="sha256:old")
        report = ReconciliationEngine().reconcile(
            [_proposal(provenance=Provenance.INFERRED, fingerprint="sha256:new")],
            _store(existing),
        )
        entry = report.entries[0]
        assert entry.decision is ReconciliationDecision.CONTRADICTION
        assert entry.existing == existing.content
        assert entry.existing_provenance is Provenance.HUMAN_CURATED
        assert not entry.auto_apply

    def test_frozen_fact_flags_contradiction_regardless_of_tier(self) -> None:
        """S3 — a frozen fact is untouched even by higher-tier conflicting evidence."""
        existing = _existing(provenance=Provenance.INFERRED, frozen=True, fingerprint="sha256:old")
        report = ReconciliationEngine().reconcile(
            [_proposal(provenance=Provenance.BOARD_APPROVED, fingerprint="sha256:new")],
            _store(existing),
        )
        entry = report.entries[0]
        assert entry.decision is ReconciliationDecision.CONTRADICTION
        assert entry.existing_frozen is True
        assert entry.existing == existing.content


# ---------------------------------------------------------------------------
# Two sources disagree in one run (S4)
# ---------------------------------------------------------------------------


class TestIntraRunContradiction:
    def test_two_disagreeing_proposals_both_appear(self) -> None:
        """S4 — both sides flagged; neither silently wins; nothing applied."""
        report = ReconciliationEngine().reconcile(
            [
                _proposal(fingerprint="sha256:a", content={"columns": [{"type": "int"}]}),
                _proposal(fingerprint="sha256:b", content={"columns": [{"type": "string"}]}),
            ],
            _store(),
        )
        assert len(report.entries) == 2
        assert all(e.decision is ReconciliationDecision.CONTRADICTION for e in report.entries)
        assert {e.proposal.content["columns"][0]["type"] for e in report.entries} == {
            "int",
            "string",
        }

    def test_duplicate_agreeing_proposals_collapse_to_one(self) -> None:
        report = ReconciliationEngine().reconcile(
            [_proposal(fingerprint="sha256:same"), _proposal(fingerprint="sha256:same")],
            _store(),
        )
        assert [e.decision for e in report.entries] == [ReconciliationDecision.ADD]


# ---------------------------------------------------------------------------
# Propose-only by default; bounded auto-apply (S5)
# ---------------------------------------------------------------------------


class TestAutoApply:
    def test_default_config_is_propose_only(self) -> None:
        """S5-AC1 — nothing is auto-apply-eligible under the default config."""
        report = ReconciliationEngine().reconcile([_proposal()], _store())
        assert report.entries[0].auto_apply is False

    def test_non_structural_edit_is_auto_apply_eligible(self) -> None:
        """S5-AC2 — an inferred, high-confidence, non-structural edit may auto-apply."""
        config = ReconcileConfig(auto_apply=AutoApplyConfig(enabled=True, min_confidence=0.95))
        existing = _existing(
            fingerprint="sha256:old",
            content={"grain": ["order_id"], "joins": [], "measures": [], "description": "old"},
        )
        proposal = _proposal(
            fingerprint="sha256:new",
            confidence=0.99,
            content={"grain": ["order_id"], "joins": [], "measures": [], "description": "new"},
        )
        report = ReconciliationEngine(config).reconcile([proposal], _store(existing))
        entry = report.entries[0]
        assert entry.decision is ReconciliationDecision.EDIT
        assert entry.auto_apply is True

    def test_grain_change_is_never_auto_applied(self) -> None:
        """S5-AC2 — a grain change stays proposed for review regardless of confidence."""
        config = ReconcileConfig(auto_apply=AutoApplyConfig(enabled=True, min_confidence=0.95))
        existing = _existing(
            fingerprint="sha256:old", content={"grain": ["order_id"], "joins": [], "measures": []}
        )
        proposal = _proposal(
            fingerprint="sha256:new",
            confidence=1.0,
            content={"grain": ["order_id", "line_id"], "joins": [], "measures": []},
        )
        report = ReconciliationEngine(config).reconcile([proposal], _store(existing))
        entry = report.entries[0]
        assert entry.decision is ReconciliationDecision.EDIT
        assert entry.auto_apply is False

    def test_confidence_below_threshold_blocks_auto_apply_and_marks_low(self) -> None:
        config = ReconcileConfig(auto_apply=AutoApplyConfig(enabled=True, min_confidence=0.95))
        existing = _existing(
            fingerprint="sha256:old",
            content={"grain": ["order_id"], "joins": [], "measures": [], "description": "old"},
        )
        proposal = _proposal(
            fingerprint="sha256:new",
            confidence=0.5,
            content={"grain": ["order_id"], "joins": [], "measures": [], "description": "new"},
        )
        entry = ReconciliationEngine(config).reconcile([proposal], _store(existing)).entries[0]
        assert entry.auto_apply is False
        assert entry.low_confidence is True


# ---------------------------------------------------------------------------
# Disappeared evidence → prune (S10)
# ---------------------------------------------------------------------------


class TestDisappearedEvidence:
    def test_inferred_target_without_proposal_is_pruned(self) -> None:
        """S10 — a previously-ingested inferred relation no longer at source is pruned."""
        gone = _existing(
            target="semantics/warehouse_pg/legacy.yaml", provenance=Provenance.INFERRED
        )
        report = ReconciliationEngine().reconcile([], _store(gone))
        assert len(report.entries) == 1
        entry = report.entries[0]
        assert entry.decision is ReconciliationDecision.PRUNE
        assert entry.target == "semantics/warehouse_pg/legacy.yaml"
        assert entry.proposal.op is ProposalOp.PRUNE

    def test_frozen_or_curated_disappeared_facts_are_not_pruned(self) -> None:
        frozen = _existing(target="semantics/warehouse_pg/frozen.yaml", frozen=True)
        curated = _existing(
            target="semantics/warehouse_pg/curated.yaml", provenance=Provenance.HUMAN_CURATED
        )
        report = ReconciliationEngine().reconcile([], _store(frozen, curated))
        assert report.entries == []

    def test_addressed_target_is_not_pruned(self) -> None:
        existing = _existing(fingerprint="sha256:same")
        report = ReconciliationEngine().reconcile(
            [_proposal(fingerprint="sha256:same")], _store(existing)
        )
        assert all(e.decision is not ReconciliationDecision.PRUNE for e in report.entries)


# ---------------------------------------------------------------------------
# Strict contradiction mode (§5.4)
# ---------------------------------------------------------------------------


class TestStrictMode:
    def test_default_never_blocks_on_contradiction(self) -> None:
        existing = _existing(provenance=Provenance.HUMAN_CURATED, fingerprint="sha256:old")
        engine = ReconciliationEngine()
        report = engine.reconcile([_proposal(fingerprint="sha256:new")], _store(existing))
        assert report.summary[ReconciliationDecision.CONTRADICTION.value] == 1
        assert engine.contradictions_block(report) is False

    def test_strict_mode_blocks_on_contradiction(self) -> None:
        existing = _existing(provenance=Provenance.HUMAN_CURATED, fingerprint="sha256:old")
        engine = ReconciliationEngine(ReconcileConfig(strict_contradictions=True))
        report = engine.reconcile([_proposal(fingerprint="sha256:new")], _store(existing))
        assert engine.contradictions_block(report) is True

    def test_strict_mode_clean_run_does_not_block(self) -> None:
        engine = ReconciliationEngine(ReconcileConfig(strict_contradictions=True))
        report = engine.reconcile([_proposal()], _store())
        assert engine.contradictions_block(report) is False


# ---------------------------------------------------------------------------
# Determinism (S9-AC1)
# ---------------------------------------------------------------------------


class TestDeterminism:
    def test_identical_inputs_yield_identical_reports(self) -> None:
        proposals = [
            _proposal(target="semantics/warehouse_pg/a.yaml", fingerprint="sha256:a"),
            _proposal(target="semantics/warehouse_pg/b.yaml", fingerprint="sha256:b"),
        ]
        facts = [
            _existing(target="semantics/warehouse_pg/gone1.yaml"),
            _existing(target="semantics/warehouse_pg/gone2.yaml"),
        ]
        first = ReconciliationEngine().reconcile(proposals, _store(*facts))
        second = ReconciliationEngine().reconcile(proposals, _store(*facts))
        assert first == second


# ---------------------------------------------------------------------------
# Integration with the real builder
# ---------------------------------------------------------------------------


class TestBuilderIntegration:
    async def test_builder_proposals_reconcile_to_add_against_empty_store(self) -> None:
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
        evidence = EvidenceItem(
            source="warehouse_pg",
            kind="relation_schema",
            acquisition_tier=AcquisitionTier.LIVE,
            payload=schema.model_dump(mode="json"),
            source_fingerprint=schema.source_fingerprint or "sha256:none",
            observed_at="2026-06-15T12:00:00Z",  # type: ignore[arg-type]
        )
        proposals = (await ContextBuilder().build([evidence])).proposals
        report = ReconciliationEngine().reconcile(proposals, _store())
        assert [e.decision for e in report.entries] == [ReconciliationDecision.ADD]

        # Re-running against the now-accepted fact is a no-op (idempotency).
        accepted = _existing(
            target=proposals[0].target, fingerprint=schema.source_fingerprint or ""
        )
        rerun = ReconciliationEngine().reconcile(proposals, _store(accepted))
        assert [e.decision for e in rerun.entries] == [ReconciliationDecision.NO_OP]


# ---------------------------------------------------------------------------
# Modeling-tier preference (SPEC-E3 §7, S6)
# ---------------------------------------------------------------------------

_COLS_DECIMAL = [{"name": "amount", "type": "decimal", "nullable": True}]
_COLS_INT = [{"name": "amount", "type": "int", "nullable": True}]


class TestModelingTierPreference:
    def test_modeling_wins_over_live_when_no_type_conflict(self) -> None:
        """AC1 default — modeling-tier evidence is preferred when column types agree."""
        modeling = _proposal(
            fingerprint="sha256:modeling",
            tier=AcquisitionTier.MODELING,
            content={"columns": _COLS_DECIMAL, "description": "curated"},
        )
        live = _proposal(
            fingerprint="sha256:live",
            tier=AcquisitionTier.LIVE,
            content={"columns": _COLS_DECIMAL},
        )
        report = ReconciliationEngine().reconcile([modeling, live], _store())
        assert len(report.entries) == 1
        entry = report.entries[0]
        assert entry.decision is ReconciliationDecision.ADD
        assert entry.proposal.acquisition_tier is AcquisitionTier.MODELING

    def test_type_conflict_flags_contradiction_with_both_tiers(self) -> None:
        """AC1 conflict — a genuine column-type disagreement surfaces as a contradiction."""
        modeling = _proposal(
            fingerprint="sha256:modeling",
            tier=AcquisitionTier.MODELING,
            content={"columns": _COLS_DECIMAL},
        )
        live = _proposal(
            fingerprint="sha256:live",
            tier=AcquisitionTier.LIVE,
            content={"columns": _COLS_INT},
        )
        report = ReconciliationEngine().reconcile([modeling, live], _store())
        assert len(report.entries) == 2
        assert all(e.decision is ReconciliationDecision.CONTRADICTION for e in report.entries)
        tiers = {e.proposal.acquisition_tier for e in report.entries}
        assert tiers == {AcquisitionTier.MODELING, AcquisitionTier.LIVE}

    def test_type_conflict_recommended_action_names_tiers(self) -> None:
        """Type-conflict contradiction message distinguishes modeling vs introspection."""
        modeling = _proposal(
            fingerprint="sha256:modeling",
            tier=AcquisitionTier.MODELING,
            content={"columns": _COLS_DECIMAL},
        )
        live = _proposal(
            fingerprint="sha256:live",
            tier=AcquisitionTier.LIVE,
            content={"columns": _COLS_INT},
        )
        report = ReconciliationEngine().reconcile([modeling, live], _store())
        action = report.entries[0].recommended_action or ""
        assert "modeling" in action
        assert "introspection" in action

    def test_provenance_boundary_modeling_never_overwrites_curated(self) -> None:
        """Provenance boundary — modeling (inferred) never replaces a human_curated fact."""
        modeling = _proposal(
            fingerprint="sha256:modeling",
            tier=AcquisitionTier.MODELING,
            content={"columns": _COLS_DECIMAL},
        )
        live = _proposal(
            fingerprint="sha256:live",
            tier=AcquisitionTier.LIVE,
            content={"columns": _COLS_DECIMAL},
        )
        curated = _existing(provenance=Provenance.HUMAN_CURATED, fingerprint="sha256:curated")
        report = ReconciliationEngine().reconcile([modeling, live], _store(curated))
        assert len(report.entries) == 1
        assert report.entries[0].decision is ReconciliationDecision.CONTRADICTION
        assert report.entries[0].existing_provenance is Provenance.HUMAN_CURATED

    def test_two_live_proposals_still_contradict(self) -> None:
        """S4 guard — two equal-rank proposals with different signatures still contradict."""
        live_a = _proposal(
            fingerprint="sha256:a",
            tier=AcquisitionTier.LIVE,
            content={"columns": _COLS_DECIMAL},
        )
        live_b = _proposal(
            fingerprint="sha256:b",
            tier=AcquisitionTier.LIVE,
            content={"columns": _COLS_INT},
        )
        report = ReconciliationEngine().reconcile([live_a, live_b], _store())
        assert len(report.entries) == 2
        assert all(e.decision is ReconciliationDecision.CONTRADICTION for e in report.entries)

    def test_two_modeling_proposals_disagree_contradict(self) -> None:
        """Two modeling-tier proposals with different types are a tie at top rank → contradiction."""
        modeling_a = _proposal(
            fingerprint="sha256:a",
            tier=AcquisitionTier.MODELING,
            content={"columns": _COLS_DECIMAL},
        )
        modeling_b = _proposal(
            fingerprint="sha256:b",
            tier=AcquisitionTier.MODELING,
            content={"columns": _COLS_INT},
        )
        report = ReconciliationEngine().reconcile([modeling_a, modeling_b], _store())
        assert len(report.entries) == 2
        assert all(e.decision is ReconciliationDecision.CONTRADICTION for e in report.entries)

    async def test_builder_propagates_modeling_tier_end_to_end(self) -> None:
        """End-to-end: builder propagates MODELING tier and it wins over a LIVE proposal."""
        cols = [ColumnInfo(name="order_id", type="int", nullable=False, position=1)]
        modeling_schema = RelationSchema(
            connection="warehouse_pg",
            relation="analytics.orders",
            kind="table",
            columns=cols,
            primary_key=["order_id"],
            foreign_keys=[],
            acquisition_tier=AcquisitionTier.MODELING,
            source_fingerprint=compute_fingerprint(cols, ["order_id"], []),
        )
        live_schema = RelationSchema(
            connection="warehouse_pg",
            relation="analytics.orders",
            kind="table",
            columns=cols,
            primary_key=["order_id"],
            foreign_keys=[],
            acquisition_tier=AcquisitionTier.LIVE,
            source_fingerprint="sha256:live-fp",
        )
        modeling_item = EvidenceItem(
            source="warehouse_pg",
            kind="relation_schema",
            acquisition_tier=AcquisitionTier.MODELING,
            payload=modeling_schema.model_dump(mode="json"),
            source_fingerprint=modeling_schema.source_fingerprint or "sha256:none",
            observed_at="2026-06-15T12:00:00Z",  # type: ignore[arg-type]
        )
        live_item = EvidenceItem(
            source="warehouse_pg",
            kind="relation_schema",
            acquisition_tier=AcquisitionTier.LIVE,
            payload=live_schema.model_dump(mode="json"),
            source_fingerprint="sha256:live-fp",
            observed_at="2026-06-15T12:00:00Z",  # type: ignore[arg-type]
        )
        proposals = (await ContextBuilder().build([modeling_item, live_item])).proposals
        assert all(p.target == "semantics/warehouse_pg/orders.yaml" for p in proposals)
        report = ReconciliationEngine().reconcile(proposals, _store())
        assert len(report.entries) == 1
        assert report.entries[0].decision is ReconciliationDecision.ADD
        assert report.entries[0].proposal.acquisition_tier is AcquisitionTier.MODELING


# ---------------------------------------------------------------------------
# refine() — async LLM post-pass for intra-run contradictions (SPEC-E10 §3)
# ---------------------------------------------------------------------------


class _StubReconcileDrafter:
    """Test stub: always picks the given winner index."""

    def __init__(self, winner: int) -> None:
        self._winner = winner

    async def draft_resolution(
        self,
        target: str,
        proposals: list[Proposal],  # noqa: ARG002
    ) -> ResolutionDraft | None:
        return ResolutionDraft(winner_index=self._winner)


class _DecliningDrafter:
    """Test stub: always returns None (declines to resolve)."""

    async def draft_resolution(
        self,
        target: str,
        proposals: list[Proposal],  # noqa: ARG002
    ) -> ResolutionDraft | None:
        return None


class _OutOfRangeDrafter:
    """Test stub: returns an index outside the proposals list."""

    async def draft_resolution(
        self,
        target: str,
        proposals: list[Proposal],  # noqa: ARG002
    ) -> ResolutionDraft | None:
        return ResolutionDraft(winner_index=99)


class TestRefine:
    async def test_null_drafter_is_no_op(self) -> None:
        """NullReconcileDrafter leaves the report unchanged."""
        p1 = _proposal(fingerprint="sha256:a", content={"grain": ["order_id"]})
        p2 = _proposal(fingerprint="sha256:b", content={"grain": ["id"]})
        engine = ReconciliationEngine(drafter=NullReconcileDrafter())
        report = engine.reconcile([p1, p2], _store())
        assert len(report.entries) == 2
        refined = await engine.refine(report, _store())
        assert len(refined.entries) == 2
        assert all(e.decision is ReconciliationDecision.CONTRADICTION for e in refined.entries)

    async def test_stub_drafter_resolves_intra_run_contradiction(self) -> None:
        """A drafter picking winner 0 replaces a 2-entry contradiction with one ADD entry."""
        p1 = _proposal(fingerprint="sha256:a", content={"grain": ["order_id"]})
        p2 = _proposal(fingerprint="sha256:b", content={"grain": ["id"]})
        engine = ReconciliationEngine(drafter=_StubReconcileDrafter(winner=0))
        report = engine.reconcile([p1, p2], _store())
        assert len(report.entries) == 2
        refined = await engine.refine(report, _store())
        assert len(refined.entries) == 1
        assert refined.entries[0].decision is ReconciliationDecision.ADD

    async def test_single_entry_contradiction_is_unchanged(self) -> None:
        """Policy/frozen contradictions (1 entry per target) are never LLM-resolved."""
        existing = _existing(provenance=Provenance.HUMAN_CURATED, fingerprint="sha256:old")
        p = _proposal(provenance=Provenance.INFERRED, fingerprint="sha256:new")
        engine = ReconciliationEngine(drafter=_StubReconcileDrafter(winner=0))
        report = engine.reconcile([p], _store(existing))
        assert len(report.entries) == 1
        assert report.entries[0].decision is ReconciliationDecision.CONTRADICTION
        refined = await engine.refine(report, _store(existing))
        assert len(refined.entries) == 1
        assert refined.entries[0].decision is ReconciliationDecision.CONTRADICTION

    async def test_declining_drafter_preserves_contradictions(self) -> None:
        """When the drafter returns None, original CONTRADICTION entries are kept."""
        p1 = _proposal(fingerprint="sha256:a", content={"grain": ["order_id"]})
        p2 = _proposal(fingerprint="sha256:b", content={"grain": ["id"]})
        engine = ReconciliationEngine(drafter=_DecliningDrafter())
        report = engine.reconcile([p1, p2], _store())
        refined = await engine.refine(report, _store())
        assert len(refined.entries) == 2
        assert all(e.decision is ReconciliationDecision.CONTRADICTION for e in refined.entries)

    async def test_out_of_range_winner_preserves_contradictions(self) -> None:
        """An out-of-range winner index is treated as a declined resolution."""
        p1 = _proposal(fingerprint="sha256:a", content={"grain": ["order_id"]})
        p2 = _proposal(fingerprint="sha256:b", content={"grain": ["id"]})
        engine = ReconciliationEngine(drafter=_OutOfRangeDrafter())
        report = engine.reconcile([p1, p2], _store())
        refined = await engine.refine(report, _store())
        assert len(refined.entries) == 2
        assert all(e.decision is ReconciliationDecision.CONTRADICTION for e in refined.entries)

    async def test_non_contradiction_entries_unchanged(self) -> None:
        """refine() passes ADD/EDIT/NO_OP entries through untouched."""
        p_add = _proposal(fingerprint="sha256:new-target", target="semantics/w/new.yaml")
        p_noop = _proposal(fingerprint="sha256:same", target="semantics/w/existing.yaml")
        existing = _existing(target="semantics/w/existing.yaml", fingerprint="sha256:same")
        engine = ReconciliationEngine(drafter=_StubReconcileDrafter(winner=0))
        report = engine.reconcile([p_add, p_noop], InMemoryAcceptedStore([existing]))
        decisions = {e.target: e.decision for e in report.entries}
        assert decisions["semantics/w/new.yaml"] is ReconciliationDecision.ADD
        assert decisions["semantics/w/existing.yaml"] is ReconciliationDecision.NO_OP
        refined = await engine.refine(report, InMemoryAcceptedStore([existing]))
        assert len(refined.entries) == 2
