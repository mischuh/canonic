"""End-to-end tests for UsageEvidence: builder + reconciliation (SPEC-E3 §3.3, §9 S3).

Covers the two acceptance criteria from GH-89:

AC1: A Metabase question encoding a different revenue definition is extracted as
     ``UsageEvidence{role: alternative}`` and proposed as a ``deprecated_alternative``
     on the existing canonical binding — canonical is never promoted or replaced (FR-13).

AC2: A frequently-run, trusted question is extracted as ``UsageEvidence{role: trusted_example}``
     and proposed as an assertion candidate (propose-only, never auto-applied).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from canon.connectors.base import (
    AcquisitionTier,
    UsageDefinition,
    UsageEvidence,
    UsageRole,
)
from canon.ingestion.builder import (
    _DA_SENTINEL,
    ContextBuilder,
    _assertion_slug,
    _metric_slug,
)
from canon.ingestion.models import (
    EvidenceItem,
    EvidenceKind,
    ProposalOp,
    ReconciliationDecision,
)
from canon.ingestion.reconciliation import (
    ExistingFact,
    InMemoryAcceptedStore,
    ReconciliationEngine,
)
from canon.semantic.models import Provenance

_NOW = datetime(2026, 6, 19, 12, 0, 0, tzinfo=UTC)


def _usage_evidence_item(
    *,
    artifact: str,
    title: str,
    expr: str,
    references: list[str],
    role: UsageRole,
    frequency: int = 0,
    source: str = "metabase_prod",
    fingerprint: str = "sha256:deadbeef",
) -> EvidenceItem:
    ev = UsageEvidence(
        source=source,
        artifact=artifact,
        title=title,
        defines=UsageDefinition(expr=expr, references=references),
        role=role,
        frequency=frequency,
        native_ref=f"metabase:{artifact}",
        source_fingerprint=fingerprint,
        observed_at=_NOW,
    )
    return EvidenceItem(
        source=source,
        kind=EvidenceKind.USAGE_EVIDENCE,
        acquisition_tier=AcquisitionTier.QUERY_HISTORY,
        payload=ev.model_dump(mode="json"),
        source_fingerprint=fingerprint,
        observed_at=_NOW,
    )


def _metric_binding_fact(
    metric: str,
    canonical_source: str,
    canonical_measure: str,
    *,
    deprecated_alternatives: list[dict[str, Any]] | None = None,
    provenance: Provenance = Provenance.HUMAN_CURATED,
) -> ExistingFact:
    slug = metric.replace(" ", "_").lower()
    content: dict[str, Any] = {
        "metric": metric,
        "canonical": {"source": canonical_source, "measure": canonical_measure},
        "provenance": provenance.value,
        "aliases": [],
        "deprecated_alternatives": deprecated_alternatives or [],
        "status": "active",
    }
    return ExistingFact(
        target=f"contracts/metrics/{slug}.yaml",
        content=content,
        provenance=provenance,
        frozen=False,
        source_fingerprint=None,
    )


class TestSlugHelpers:
    def test_metric_slug_lowercases_and_replaces_spaces(self) -> None:
        assert _metric_slug("Gross Revenue") == "gross_revenue"

    def test_metric_slug_strips_special_chars(self) -> None:
        assert _metric_slug("Revenue (incl. refunds)") == "revenue_incl_refunds"

    def test_metric_slug_caps_at_64_chars(self) -> None:
        long_title = "a" * 100
        assert len(_metric_slug(long_title)) <= 64

    def test_assertion_slug_combines_source_and_artifact(self) -> None:
        slug = _assertion_slug("metabase_prod", "question:412")
        assert "metabase_prod" in slug
        assert "question" in slug


class TestBuilderAlternativeProposal:
    async def test_ac1_alternative_produces_edit_proposal(self) -> None:
        item = _usage_evidence_item(
            artifact="question:412",
            title="Gross revenue (incl. refunds)",
            expr="SELECT sum(amount) FROM analytics.fct_orders",
            references=["analytics.fct_orders"],
            role=UsageRole.ALTERNATIVE,
            frequency=87,
        )
        builder = ContextBuilder()
        result = await builder.build([item])

        assert len(result.proposals) == 1
        proposal = result.proposals[0]
        assert proposal.op is ProposalOp.EDIT
        assert proposal.target.startswith("contracts/metrics/")
        assert proposal.provenance is Provenance.INFERRED

    async def test_alternative_proposal_contains_da_sentinel(self) -> None:
        item = _usage_evidence_item(
            artifact="question:412",
            title="Gross Revenue",
            expr="sum(amount)",
            references=[],
            role=UsageRole.ALTERNATIVE,
        )
        builder = ContextBuilder()
        result = await builder.build([item])
        proposal = result.proposals[0]
        assert _DA_SENTINEL in proposal.content

    async def test_da_fragment_has_correct_fields(self) -> None:
        item = _usage_evidence_item(
            artifact="question:412",
            title="Gross Revenue",
            expr="sum(amount)",
            references=[],
            role=UsageRole.ALTERNATIVE,
        )
        builder = ContextBuilder()
        result = await builder.build([item])
        da = result.proposals[0].content[_DA_SENTINEL]
        assert da["source"] == "metabase_prod"
        assert da["ref"] == "question:412"
        assert da["reason"] == "Gross Revenue"

    async def test_alternative_proposal_never_contains_canonical(self) -> None:
        item = _usage_evidence_item(
            artifact="question:412",
            title="Revenue (evil version)",
            expr="sum(bad_col)",
            references=[],
            role=UsageRole.ALTERNATIVE,
        )
        builder = ContextBuilder()
        result = await builder.build([item])
        proposal = result.proposals[0]
        content_str = str(proposal.content)
        assert "canonical" not in content_str

    async def test_alternative_anchored_to_fingerprint(self) -> None:
        item = _usage_evidence_item(
            artifact="question:412",
            title="Revenue",
            expr="sum(x)",
            references=[],
            role=UsageRole.ALTERNATIVE,
            fingerprint="sha256:abc123",
        )
        builder = ContextBuilder()
        result = await builder.build([item])
        assert "sha256:abc123" in result.proposals[0].anchored_to


class TestBuilderTrustedExampleProposal:
    async def test_ac2_trusted_example_produces_add_proposal(self) -> None:
        item = _usage_evidence_item(
            artifact="question:413",
            title="Net Revenue (official)",
            expr="sum(net_amount)",
            references=["analytics.fct_orders"],
            role=UsageRole.TRUSTED_EXAMPLE,
            frequency=250,
        )
        builder = ContextBuilder()
        result = await builder.build([item])

        assert len(result.proposals) == 1
        proposal = result.proposals[0]
        assert proposal.op is ProposalOp.ADD
        assert proposal.target.startswith("contracts/assertions/")

    async def test_trusted_example_content_has_assertion_shape(self) -> None:
        item = _usage_evidence_item(
            artifact="question:413",
            title="Net Revenue",
            expr="sum(net_amount)",
            references=["analytics.fct_orders"],
            role=UsageRole.TRUSTED_EXAMPLE,
        )
        builder = ContextBuilder()
        result = await builder.build([item])
        content = result.proposals[0].content
        assert "id" in content
        assert "query" in content
        assert "expect" in content
        assert "source_of_truth" in content

    async def test_trusted_example_source_of_truth_is_native_ref(self) -> None:
        item = _usage_evidence_item(
            artifact="question:413",
            title="Net Revenue",
            expr="sum(net_amount)",
            references=[],
            role=UsageRole.TRUSTED_EXAMPLE,
        )
        builder = ContextBuilder()
        result = await builder.build([item])
        sot = result.proposals[0].content["source_of_truth"]
        assert "metabase" in sot
        assert "question:413" in sot

    async def test_trusted_example_proposal_never_contains_canonical_ref(self) -> None:
        item = _usage_evidence_item(
            artifact="question:413",
            title="Net Revenue",
            expr="sum(x)",
            references=[],
            role=UsageRole.TRUSTED_EXAMPLE,
        )
        builder = ContextBuilder()
        result = await builder.build([item])
        content_str = str(result.proposals[0].content)
        assert "canonical" not in content_str


class TestReconciliationAlternativeMerge:
    """AC1: deprecated_alternative merged onto existing binding; canonical never replaced."""

    async def test_ac1_merges_da_into_existing_binding(self) -> None:
        item = _usage_evidence_item(
            artifact="question:412",
            title="Gross Revenue",
            expr="sum(amount)",
            references=[],
            role=UsageRole.ALTERNATIVE,
        )
        builder = ContextBuilder()
        result = await builder.build([item])
        proposal = result.proposals[0]

        existing = _metric_binding_fact(
            "Gross Revenue",
            canonical_source="dbt_prod",
            canonical_measure="gross_revenue",
        )
        store = InMemoryAcceptedStore([existing])
        engine = ReconciliationEngine()
        report = engine.reconcile([proposal], store)

        assert len(report.entries) == 1
        entry = report.entries[0]
        assert entry.decision is ReconciliationDecision.EDIT

    async def test_ac1_canonical_not_in_merged_proposal(self) -> None:
        item = _usage_evidence_item(
            artifact="question:412",
            title="Gross Revenue",
            expr="sum(amount)",
            references=[],
            role=UsageRole.ALTERNATIVE,
        )
        builder = ContextBuilder()
        result = await builder.build([item])
        proposal = result.proposals[0]

        existing = _metric_binding_fact(
            "Gross Revenue",
            canonical_source="dbt_prod",
            canonical_measure="gross_revenue",
        )
        store = InMemoryAcceptedStore([existing])
        engine = ReconciliationEngine()
        report = engine.reconcile([proposal], store)

        entry = report.entries[0]
        # The merged proposal content must preserve the canonical binding unchanged
        merged_content = entry.proposal.content
        assert merged_content["canonical"]["source"] == "dbt_prod"
        assert merged_content["canonical"]["measure"] == "gross_revenue"

    async def test_ac1_deprecated_alternative_added_to_list(self) -> None:
        item = _usage_evidence_item(
            artifact="question:412",
            title="Gross Revenue",
            expr="sum(amount)",
            references=[],
            role=UsageRole.ALTERNATIVE,
        )
        builder = ContextBuilder()
        result = await builder.build([item])
        proposal = result.proposals[0]

        existing = _metric_binding_fact(
            "Gross Revenue",
            canonical_source="dbt_prod",
            canonical_measure="gross_revenue",
        )
        store = InMemoryAcceptedStore([existing])
        engine = ReconciliationEngine()
        report = engine.reconcile([proposal], store)

        merged_das = report.entries[0].proposal.content["deprecated_alternatives"]
        assert len(merged_das) == 1
        assert merged_das[0]["ref"] == "question:412"
        assert merged_das[0]["source"] == "metabase_prod"

    async def test_ac1_idempotent_on_same_ref(self) -> None:
        existing_das = [{"source": "metabase_prod", "ref": "question:412", "reason": "gross"}]
        item = _usage_evidence_item(
            artifact="question:412",
            title="Gross Revenue",
            expr="sum(amount)",
            references=[],
            role=UsageRole.ALTERNATIVE,
        )
        builder = ContextBuilder()
        result = await builder.build([item])
        proposal = result.proposals[0]

        existing = _metric_binding_fact(
            "Gross Revenue",
            canonical_source="dbt_prod",
            canonical_measure="gross_revenue",
            deprecated_alternatives=existing_das,
        )
        store = InMemoryAcceptedStore([existing])
        engine = ReconciliationEngine()
        report = engine.reconcile([proposal], store)

        # Already in list → no-op, not a duplicate.
        assert report.entries[0].decision is ReconciliationDecision.NO_OP

    async def test_ac1_no_match_creates_no_canonical_binding(self) -> None:
        item = _usage_evidence_item(
            artifact="question:412",
            title="Revenue Without Canonical",
            expr="sum(x)",
            references=[],
            role=UsageRole.ALTERNATIVE,
        )
        builder = ContextBuilder()
        result = await builder.build([item])
        proposal = result.proposals[0]

        # Empty store — no existing metric binding.
        store = InMemoryAcceptedStore([])
        engine = ReconciliationEngine()
        report = engine.reconcile([proposal], store)

        # Without an existing binding, no canonical is created.
        # The decision is ADD (trying to add content that has the DA sentinel, not a full binding).
        # The sentinel key is in the content — no canonical ref present.
        entry = report.entries[0]
        content_str = str(entry.proposal.content)
        assert "canonical" not in content_str


class TestReconciliationTrustedExampleProposal:
    """AC2: trusted_example produces assertion candidate; propose-only, never auto-applied."""

    async def test_ac2_trusted_example_adds_assertion_file(self) -> None:
        item = _usage_evidence_item(
            artifact="question:413",
            title="Net Revenue (official)",
            expr="sum(net_amount)",
            references=["analytics.fct_orders"],
            role=UsageRole.TRUSTED_EXAMPLE,
            frequency=250,
        )
        builder = ContextBuilder()
        result = await builder.build([item])
        proposal = result.proposals[0]

        store = InMemoryAcceptedStore([])
        engine = ReconciliationEngine()
        report = engine.reconcile([proposal], store)

        assert len(report.entries) == 1
        entry = report.entries[0]
        assert entry.decision is ReconciliationDecision.ADD
        assert entry.target.startswith("contracts/assertions/")

    async def test_ac2_assertion_candidate_is_not_auto_applied(self) -> None:
        item = _usage_evidence_item(
            artifact="question:413",
            title="Net Revenue (official)",
            expr="sum(net_amount)",
            references=[],
            role=UsageRole.TRUSTED_EXAMPLE,
        )
        builder = ContextBuilder()
        result = await builder.build([item])
        proposal = result.proposals[0]

        store = InMemoryAcceptedStore([])
        engine = ReconciliationEngine()
        report = engine.reconcile([proposal], store)

        # propose-only — never auto-applied under default policy.
        assert not report.entries[0].auto_apply

    async def test_ac2_assertion_content_has_no_canonical_ref(self) -> None:
        item = _usage_evidence_item(
            artifact="question:413",
            title="Net Revenue",
            expr="sum(x)",
            references=[],
            role=UsageRole.TRUSTED_EXAMPLE,
        )
        builder = ContextBuilder()
        result = await builder.build([item])
        content = result.proposals[0].content
        assert "canonical" not in str(content)

    async def test_ac2_assertion_provenance_is_inferred(self) -> None:
        item = _usage_evidence_item(
            artifact="question:413",
            title="Net Revenue",
            expr="sum(x)",
            references=[],
            role=UsageRole.TRUSTED_EXAMPLE,
        )
        builder = ContextBuilder()
        result = await builder.build([item])
        assert result.proposals[0].provenance is Provenance.INFERRED


class TestUsageEvidenceKindRegistration:
    def test_usage_evidence_is_a_known_kind(self) -> None:
        from canon.ingestion.models import KNOWN_EVIDENCE_KINDS, EvidenceKind

        assert EvidenceKind.USAGE_EVIDENCE in KNOWN_EVIDENCE_KINDS
        assert "usage_evidence" in KNOWN_EVIDENCE_KINDS

    async def test_builder_does_not_skip_usage_evidence(self) -> None:
        item = _usage_evidence_item(
            artifact="question:1",
            title="Revenue",
            expr="sum(x)",
            references=[],
            role=UsageRole.ALTERNATIVE,
        )
        builder = ContextBuilder()
        result = await builder.build([item])
        assert not result.skipped
        assert len(result.proposals) == 1
