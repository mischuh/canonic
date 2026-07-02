"""Acceptance-criteria tests for ingest-time reference pruning (GH-48, SPEC-E6 §3.2).

Covers S5 AC1: a disappeared entity yields a propose-only diff that removes the stale ref
and downgrades freshness — never a silent edit, never a dangling ref.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

from canonic.ingestion.models import DraftedBy, ProposalOp
from canonic.knowledge.models import KnowledgePageMeta, KnowledgeScope
from canonic.knowledge.pruning import PruneAdvisor
from canonic.semantic.models import Provenance

if TYPE_CHECKING:
    from collections.abc import Callable

    from canonic.knowledge.models import KnowledgePage
    from canonic.knowledge.validation import EntityIndex, PageIndex

# A live sl_ref and page ref present in the shared `entity_index` / `page_index` fixtures.
_LIVE_SL = "warehouse_pg.orders.total_revenue"
_LIVE_REF = "test-account-policy"
# Targets the fixtures do not contain — i.e. disappeared at ingest.
_GHOST_SL = "warehouse_pg.orders.ghost_metric"
_GHOST_REF = "no-such-page"


# --- stale-detection helpers in isolation ---------------------------------------------


def test_stale_sl_refs_returns_only_disappeared_subset(
    entity_index: EntityIndex,
    make_page: Callable[..., KnowledgePage],
) -> None:
    page = make_page(sl_refs=[_LIVE_SL, _GHOST_SL, "warehouse_pg.orders.also_gone"])
    assert PruneAdvisor.stale_sl_refs(page, entity_index) == [
        _GHOST_SL,
        "warehouse_pg.orders.also_gone",
    ]


def test_stale_sl_refs_clean_page_is_empty(
    entity_index: EntityIndex,
    make_page: Callable[..., KnowledgePage],
) -> None:
    page = make_page(sl_refs=[_LIVE_SL, "warehouse_pg.customers"])
    assert PruneAdvisor.stale_sl_refs(page, entity_index) == []


def test_stale_refs_respects_scope_visibility(
    page_index: PageIndex,
    make_page: Callable[..., KnowledgePage],
) -> None:
    # A GLOBAL page referencing a USER-only page: not visible → stale, matching the validator.
    page = make_page(scope=KnowledgeScope.GLOBAL, refs=[_LIVE_REF, "my-private-note", _GHOST_REF])
    assert PruneAdvisor.stale_refs(page, page_index) == ["my-private-note", _GHOST_REF]


# --- one stale sl_ref → one Proposal (S5 AC1) -----------------------------------------


def test_one_stale_sl_ref_produces_one_proposal(
    entity_index: EntityIndex,
    make_page: Callable[..., KnowledgePage],
) -> None:
    page = make_page(sl_refs=[_LIVE_SL, _GHOST_SL])
    advisor = PruneAdvisor()

    stale_sl = advisor.stale_sl_refs(page, entity_index)
    proposal = advisor.propose_prune(page, stale_sl, [])

    assert proposal is not None
    assert proposal.op is ProposalOp.PRUNE
    assert proposal.provenance is Provenance.INFERRED
    assert proposal.drafted_by is DraftedBy.DETERMINISTIC
    assert proposal.target == str(page.path)
    # Stale ref removed, live ref retained.
    assert proposal.content["sl_refs"] == [_LIVE_SL]
    # Freshness downgraded.
    assert proposal.content["meta"]["last_validated_at"] is None


def test_proposal_anchors_to_disappeared_entity_fingerprint(
    entity_index: EntityIndex,
    make_page: Callable[..., KnowledgePage],
) -> None:
    page = make_page(
        sl_refs=[_GHOST_SL],
        meta=KnowledgePageMeta(
            last_validated_at=datetime(2026, 6, 14, tzinfo=UTC),
            bound_fingerprints={_GHOST_SL: "sha256:deadbeef"},
        ),
    )
    advisor = PruneAdvisor()
    proposal = advisor.propose_prune(page, advisor.stale_sl_refs(page, entity_index), [])

    assert proposal is not None
    assert proposal.anchored_to == ["sha256:deadbeef"]


def test_proposal_anchored_to_empty_without_recorded_fingerprint(
    entity_index: EntityIndex,
    make_page: Callable[..., KnowledgePage],
) -> None:
    page = make_page(sl_refs=[_GHOST_SL])  # no bound_fingerprints recorded
    advisor = PruneAdvisor()
    proposal = advisor.propose_prune(page, advisor.stale_sl_refs(page, entity_index), [])

    assert proposal is not None
    assert proposal.anchored_to == []


# --- multiple stale refs → a single diff ----------------------------------------------


def test_multiple_stale_refs_produce_single_proposal(
    entity_index: EntityIndex,
    page_index: PageIndex,
    make_page: Callable[..., KnowledgePage],
) -> None:
    page = make_page(
        sl_refs=[_LIVE_SL, _GHOST_SL],
        refs=[_LIVE_REF, _GHOST_REF],
        meta=KnowledgePageMeta(bound_fingerprints={_GHOST_SL: "sha256:abc"}),
    )
    advisor = PruneAdvisor()

    stale_sl = advisor.stale_sl_refs(page, entity_index)
    stale_refs = advisor.stale_refs(page, page_index)
    proposal = advisor.propose_prune(page, stale_sl, stale_refs)

    assert proposal is not None
    # One diff carrying both kinds of removal, survivors retained.
    assert proposal.content["sl_refs"] == [_LIVE_SL]
    assert proposal.content["refs"] == [_LIVE_REF]
    assert proposal.content["meta"]["last_validated_at"] is None
    assert proposal.anchored_to == ["sha256:abc"]


# --- clean page → no proposal ---------------------------------------------------------


def test_clean_page_produces_no_proposal(make_page: Callable[..., KnowledgePage]) -> None:
    page = make_page(sl_refs=[_LIVE_SL], refs=[_LIVE_REF])
    assert PruneAdvisor().propose_prune(page, [], []) is None


# --- broken page-to-page ref handled same as sl_ref -----------------------------------


def test_broken_page_ref_pruned_like_sl_ref(
    page_index: PageIndex,
    make_page: Callable[..., KnowledgePage],
) -> None:
    page = make_page(refs=[_LIVE_REF, _GHOST_REF])
    advisor = PruneAdvisor()

    stale_refs = advisor.stale_refs(page, page_index)
    proposal = advisor.propose_prune(page, [], stale_refs)

    assert proposal is not None
    assert proposal.op is ProposalOp.PRUNE
    assert proposal.content["refs"] == [_LIVE_REF]
    assert proposal.content["meta"]["last_validated_at"] is None
