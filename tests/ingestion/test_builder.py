"""Tests for canon/ingestion/builder.py (GH-33)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from canon.connectors.base import (
    AcquisitionTier,
    ColumnInfo,
    ForeignKey,
    ForeignKeyRef,
    RelationSchema,
    compute_fingerprint,
)
from canon.ingestion.builder import (
    LLM_GRAIN_CONFIDENCE,
    BuildResult,
    ContextBuilder,
    GrainDraft,
    NullLLMDrafter,
    SkippedEvidence,
)
from canon.ingestion.models import DraftedBy, EvidenceItem, ProposalOp
from canon.semantic.models import Provenance, Relationship

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_NOW = datetime(2026, 6, 15, 12, 0, 0, tzinfo=UTC)


def _columns() -> list[ColumnInfo]:
    return [
        ColumnInfo(name="order_id", type="int", nullable=False, position=1),
        ColumnInfo(name="customer_id", type="int", nullable=False, position=2),
        ColumnInfo(name="amount", type="decimal", nullable=True, position=3),
    ]


def _relation_schema(
    *,
    primary_key: list[str] | None = None,
    foreign_keys: list[ForeignKey] | None = None,
) -> RelationSchema:
    cols = _columns()
    pk = primary_key if primary_key is not None else ["order_id"]
    fks = foreign_keys if foreign_keys is not None else []
    return RelationSchema(
        connection="warehouse_pg",
        relation="analytics.fct_orders",
        kind="table",
        columns=cols,
        primary_key=pk,
        foreign_keys=fks,
        acquisition_tier=AcquisitionTier.LIVE,
        source_fingerprint=compute_fingerprint(cols, pk, fks),
    )


def _evidence(schema: RelationSchema, kind: str = "relation_schema") -> EvidenceItem:
    return EvidenceItem(
        source=schema.connection,
        kind=kind,
        acquisition_tier=AcquisitionTier.LIVE,
        payload=schema.model_dump(mode="json"),
        source_fingerprint=schema.source_fingerprint or "sha256:none",
        observed_at=_NOW,
    )


def _customer_fk() -> ForeignKey:
    return ForeignKey(
        columns=["customer_id"],
        references=ForeignKeyRef(relation="analytics.dim_customers", columns=["id"]),
    )


# ---------------------------------------------------------------------------
# Deterministic core — RelationSchema with a primary key (AC1)
# ---------------------------------------------------------------------------


class TestDeterministicCore:
    async def test_pk_relation_produces_deterministic_proposal(self) -> None:
        """AC1 — PK-bearing relation maps to a fully deterministic proposal."""
        schema = _relation_schema(foreign_keys=[_customer_fk()])
        result = await ContextBuilder().build([_evidence(schema)])

        assert len(result.proposals) == 1
        assert result.skipped == []
        p = result.proposals[0]
        assert p.op is ProposalOp.ADD
        assert p.provenance is Provenance.INFERRED
        assert p.drafted_by is DraftedBy.DETERMINISTIC
        assert p.confidence == 1.0
        assert p.anchored_to == [schema.source_fingerprint]

    async def test_target_derived_from_connection_and_relation(self) -> None:
        result = await ContextBuilder().build([_evidence(_relation_schema())])
        assert result.proposals[0].target == "semantics/warehouse_pg/fct_orders.yaml"

    async def test_content_carries_grain_columns_and_meta(self) -> None:
        schema = _relation_schema()
        content = (await ContextBuilder().build([_evidence(schema)])).proposals[0].content

        assert content["name"] == "fct_orders"
        assert content["connection"] == "warehouse_pg"
        assert content["table"] == "analytics.fct_orders"
        assert content["grain"] == ["order_id"]
        assert content["columns"] == [
            {"name": "order_id", "type": "int", "nullable": False},
            {"name": "customer_id", "type": "int", "nullable": False},
            {"name": "amount", "type": "decimal", "nullable": True},
        ]
        assert content["meta"]["source_fingerprint"] == schema.source_fingerprint
        assert "grain_draft" not in content["meta"]

    async def test_joins_derived_from_foreign_keys(self) -> None:
        schema = _relation_schema(foreign_keys=[_customer_fk()])
        joins = (await ContextBuilder().build([_evidence(schema)])).proposals[0].content["joins"]

        assert joins == [
            {
                "to": "dim_customers",
                "on": "fct_orders.customer_id = dim_customers.id",
                "relationship": Relationship.MANY_TO_ONE.value,
            }
        ]

    async def test_composite_foreign_key_joined_with_and(self) -> None:
        fk = ForeignKey(
            columns=["customer_id", "region_id"],
            references=ForeignKeyRef(relation="analytics.dim_customers", columns=["id", "region"]),
        )
        schema = _relation_schema(foreign_keys=[fk])
        join = (await ContextBuilder().build([_evidence(schema)])).proposals[0].content["joins"][0]
        assert join["on"] == (
            "fct_orders.customer_id = dim_customers.id "
            "AND fct_orders.region_id = dim_customers.region"
        )


# ---------------------------------------------------------------------------
# LLM-assisted drafting — RelationSchema without a primary key (AC2 / S1-AC2)
# ---------------------------------------------------------------------------


class TestNoPrimaryKey:
    async def test_no_pk_yields_llm_drafted_grain(self) -> None:
        """AC2 — no PK ⇒ labelled LLM draft, reduced confidence, grain not asserted."""
        schema = _relation_schema(primary_key=[])
        p = (await ContextBuilder().build([_evidence(schema)])).proposals[0]

        assert p.drafted_by is DraftedBy.LLM
        assert p.confidence < 1.0
        assert p.confidence == LLM_GRAIN_CONFIDENCE
        assert p.content["grain"] == []
        assert p.content["meta"]["grain_draft"] is True

    async def test_injected_drafter_is_used(self) -> None:
        class _StubDrafter(NullLLMDrafter):
            async def draft_grain(self, schema: RelationSchema) -> GrainDraft:
                return GrainDraft(grain=["order_id"], confidence=0.6)

        schema = _relation_schema(primary_key=[])
        p = (await ContextBuilder(llm_drafter=_StubDrafter()).build([_evidence(schema)])).proposals[
            0
        ]
        assert p.content["grain"] == ["order_id"]
        assert p.confidence == 0.6
        assert p.drafted_by is DraftedBy.LLM


# ---------------------------------------------------------------------------
# Determinism (AC3 / S9-AC1)
# ---------------------------------------------------------------------------


class TestDeterminism:
    async def test_identical_evidence_yields_identical_proposals(self) -> None:
        schema = _relation_schema(foreign_keys=[_customer_fk()])
        first = await ContextBuilder().build([_evidence(schema)])
        second = await ContextBuilder().build([_evidence(schema)])
        assert first.proposals == second.proposals


# ---------------------------------------------------------------------------
# Unknown / unhandled kinds (AC4 / S1-AC4)
# ---------------------------------------------------------------------------


class TestSkipping:
    async def test_unknown_kind_skipped_without_exception(self) -> None:
        schema = _relation_schema()
        result = await ContextBuilder().build([_evidence(schema, kind="answer_outcome")])

        assert result.proposals == []
        assert result.skipped == [
            SkippedEvidence(
                source="warehouse_pg", kind="answer_outcome", reason="unknown evidence kind"
            )
        ]

    async def test_known_kind_without_handler_skipped(self) -> None:
        schema = _relation_schema()
        result = await ContextBuilder().build([_evidence(schema, kind="observed_query")])
        assert result.proposals == []
        assert len(result.skipped) == 1
        assert result.skipped[0].kind == "observed_query"

    async def test_mixed_batch_builds_and_skips(self) -> None:
        schema = _relation_schema()
        result = await ContextBuilder().build(
            [
                _evidence(schema),
                _evidence(schema, kind="doc_evidence"),
                _evidence(schema, kind="totally_unknown"),
            ]
        )
        assert len(result.proposals) == 1
        assert {s.kind for s in result.skipped} == {"doc_evidence", "totally_unknown"}

    async def test_empty_evidence_is_empty_result(self) -> None:
        result = await ContextBuilder().build([])
        assert result == BuildResult()


# ---------------------------------------------------------------------------
# NullLLMDrafter stub
# ---------------------------------------------------------------------------


class TestNullLLMDrafter:
    async def test_draft_grain_is_empty_candidate(self) -> None:
        draft = await NullLLMDrafter().draft_grain(_relation_schema(primary_key=[]))
        assert draft.grain == []
        assert draft.confidence == LLM_GRAIN_CONFIDENCE

    async def test_draft_joins_is_empty(self) -> None:
        observed: dict[str, Any] = {"joins_observed": [{"a": "b"}]}
        assert await NullLLMDrafter().draft_joins(observed) == []
