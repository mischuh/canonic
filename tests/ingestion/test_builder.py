"""Tests for canon/ingestion/builder.py (GH-33)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from canon.connectors.base import (
    AcquisitionTier,
    ColumnInfo,
    DefinitionEntityType,
    ForeignKey,
    ForeignKeyRef,
    RelationSchema,
    compute_fingerprint,
)
from canon.ingestion.builder import (
    LLM_GRAIN_CONFIDENCE,
    LLM_JOIN_CONFIDENCE_CEILING,
    BuildResult,
    ContextBuilder,
    DimensionEnrichment,
    GrainDraft,
    JoinDraft,
    NullLLMDrafter,
    SkippedEvidence,
)
from canon.ingestion.models import DraftedBy, EvidenceItem, EvidenceKind, ProposalOp
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
                return GrainDraft(
                    grain=["order_id"], confidence=0.6, reasoning="order_id is the surrogate key"
                )

        schema = _relation_schema(primary_key=[])
        p = (await ContextBuilder(llm_drafter=_StubDrafter()).build([_evidence(schema)])).proposals[
            0
        ]
        assert p.content["grain"] == ["order_id"]
        assert p.confidence == 0.6
        assert p.drafted_by is DraftedBy.LLM
        assert p.content["meta"]["grain_reasoning"] == "order_id is the surrogate key"

    async def test_entity_grain_used_when_no_pk(self) -> None:
        """Modeling-tier ENTITY evidence provides grain for a schema with no declared PK."""
        schema = _relation_schema(primary_key=[])
        entity_item = EvidenceItem(
            source="dbt_prod",
            kind=EvidenceKind.DEFINITION,
            acquisition_tier=AcquisitionTier.MODELING,
            payload={
                "entity": "fct_orders",
                "entity_type": DefinitionEntityType.ENTITY,
                "grain": ["order_id"],
                "references": ["analytics.fct_orders"],
                "native_ref": "semantic_model.analytics.fct_orders",
                "source": "dbt_prod",
                "acquisition_tier": AcquisitionTier.MODELING,
            },
            source_fingerprint="sha256:entity-stub",
            observed_at=_NOW,
        )
        p = (await ContextBuilder().build([_evidence(schema), entity_item])).proposals[0]

        assert p.drafted_by is DraftedBy.DETERMINISTIC
        assert p.confidence == 1.0
        assert p.content["grain"] == ["order_id"]
        assert "grain_draft" not in p.content["meta"]

    async def test_pk_takes_precedence_over_entity_grain(self) -> None:
        """Declared PK wins over ENTITY evidence grain."""
        schema = _relation_schema(primary_key=["order_id"])
        entity_item = EvidenceItem(
            source="dbt_prod",
            kind=EvidenceKind.DEFINITION,
            acquisition_tier=AcquisitionTier.MODELING,
            payload={
                "entity": "fct_orders",
                "entity_type": DefinitionEntityType.ENTITY,
                "grain": ["surrogate_key"],
                "references": ["analytics.fct_orders"],
                "native_ref": "semantic_model.analytics.fct_orders",
                "source": "dbt_prod",
                "acquisition_tier": AcquisitionTier.MODELING,
            },
            source_fingerprint="sha256:entity-stub",
            observed_at=_NOW,
        )
        p = (await ContextBuilder().build([_evidence(schema), entity_item])).proposals[0]

        assert p.content["grain"] == ["order_id"]
        assert p.drafted_by is DraftedBy.DETERMINISTIC


# ---------------------------------------------------------------------------
# LLM-drafted dimension labels/aliases (bootstrap task expansion)
# ---------------------------------------------------------------------------


def _schema_with_status_dimension() -> RelationSchema:
    cols = [
        ColumnInfo(name="order_id", type="int", nullable=False, position=1),
        ColumnInfo(name="status", type="string", nullable=True, position=2),
    ]
    return RelationSchema(
        connection="warehouse_pg",
        relation="analytics.fct_orders",
        kind="table",
        columns=cols,
        primary_key=["order_id"],
        acquisition_tier=AcquisitionTier.LIVE,
        source_fingerprint=compute_fingerprint(cols, ["order_id"], []),
    )


class TestDimensionEnrichment:
    async def test_label_applied_when_over_threshold(self) -> None:
        class _StubDrafter(NullLLMDrafter):
            async def draft_dimension_labels(
                self, schema: RelationSchema, dimensions: list[dict[str, Any]]
            ) -> list[DimensionEnrichment]:
                return [DimensionEnrichment(name="status", label="Order Status", confidence=0.6)]

        schema = _schema_with_status_dimension()
        p = (await ContextBuilder(llm_drafter=_StubDrafter()).build([_evidence(schema)])).proposals[
            0
        ]
        dim = next(d for d in p.content["dimensions"] if d["name"] == "status")
        assert dim["label"] == "Order Status"
        assert dim.get("aliases") in (None, [])

    async def test_label_withheld_when_under_threshold(self) -> None:
        class _StubDrafter(NullLLMDrafter):
            async def draft_dimension_labels(
                self, schema: RelationSchema, dimensions: list[dict[str, Any]]
            ) -> list[DimensionEnrichment]:
                return [DimensionEnrichment(name="status", label="Order Status", confidence=0.2)]

        schema = _schema_with_status_dimension()
        p = (await ContextBuilder(llm_drafter=_StubDrafter()).build([_evidence(schema)])).proposals[
            0
        ]
        dim = next(d for d in p.content["dimensions"] if d["name"] == "status")
        assert "label" not in dim

    async def test_aliases_need_stricter_threshold_than_label(self) -> None:
        class _StubDrafter(NullLLMDrafter):
            async def draft_dimension_labels(
                self, schema: RelationSchema, dimensions: list[dict[str, Any]]
            ) -> list[DimensionEnrichment]:
                return [
                    DimensionEnrichment(
                        name="status",
                        label="Order Status",
                        aliases=["order_state"],
                        confidence=0.6,
                    )
                ]

        schema = _schema_with_status_dimension()
        p = (await ContextBuilder(llm_drafter=_StubDrafter()).build([_evidence(schema)])).proposals[
            0
        ]
        dim = next(d for d in p.content["dimensions"] if d["name"] == "status")
        assert dim["label"] == "Order Status"
        assert "aliases" not in dim

    async def test_aliases_applied_when_over_stricter_threshold(self) -> None:
        class _StubDrafter(NullLLMDrafter):
            async def draft_dimension_labels(
                self, schema: RelationSchema, dimensions: list[dict[str, Any]]
            ) -> list[DimensionEnrichment]:
                return [
                    DimensionEnrichment(
                        name="status",
                        label="Order Status",
                        aliases=["order_state"],
                        confidence=0.9,
                    )
                ]

        schema = _schema_with_status_dimension()
        p = (await ContextBuilder(llm_drafter=_StubDrafter()).build([_evidence(schema)])).proposals[
            0
        ]
        dim = next(d for d in p.content["dimensions"] if d["name"] == "status")
        assert dim["aliases"] == ["order_state"]

    async def test_unmatched_dimension_name_is_ignored(self) -> None:
        class _StubDrafter(NullLLMDrafter):
            async def draft_dimension_labels(
                self, schema: RelationSchema, dimensions: list[dict[str, Any]]
            ) -> list[DimensionEnrichment]:
                return [DimensionEnrichment(name="nonexistent", label="Nope", confidence=1.0)]

        schema = _schema_with_status_dimension()
        p = (await ContextBuilder(llm_drafter=_StubDrafter()).build([_evidence(schema)])).proposals[
            0
        ]
        dim = next(d for d in p.content["dimensions"] if d["name"] == "status")
        assert "label" not in dim

    async def test_null_llm_drafter_leaves_dimensions_unlabeled(self) -> None:
        """Headless/default path: NullLLMDrafter is a no-op, matching today's behavior."""
        schema = _schema_with_status_dimension()
        p = (await ContextBuilder().build([_evidence(schema)])).proposals[0]
        dim = next(d for d in p.content["dimensions"] if d["name"] == "status")
        assert dim == {"name": "status", "column": "status"}

    async def test_no_dimensions_skips_drafter_call(self) -> None:
        """No dimension columns ⇒ draft_dimension_labels is never invoked."""
        called = False

        class _StubDrafter(NullLLMDrafter):
            async def draft_dimension_labels(
                self, schema: RelationSchema, dimensions: list[dict[str, Any]]
            ) -> list[DimensionEnrichment]:
                nonlocal called
                called = True
                return []

        schema = _relation_schema(foreign_keys=[_customer_fk()])
        await ContextBuilder(llm_drafter=_StubDrafter()).build([_evidence(schema)])
        assert called is False


# ---------------------------------------------------------------------------
# LLM-drafted FK-less joins (star/snowflake bootstrap task expansion)
# ---------------------------------------------------------------------------


def _orders_schema() -> RelationSchema:
    cols = [
        ColumnInfo(name="order_id", type="int", nullable=False),
        ColumnInfo(name="category_key", type="int", nullable=True),
    ]
    return RelationSchema(
        connection="warehouse_pg",
        relation="analytics.fct_orders",
        kind="table",
        columns=cols,
        primary_key=["order_id"],
        acquisition_tier=AcquisitionTier.LIVE,
        source_fingerprint=compute_fingerprint(cols, ["order_id"], []),
    )


def _categories_schema() -> RelationSchema:
    cols = [
        ColumnInfo(name="category_key", type="int", nullable=False),
        ColumnInfo(name="label", type="string", nullable=True),
    ]
    return RelationSchema(
        connection="warehouse_pg",
        relation="analytics.dim_categories",
        kind="table",
        columns=cols,
        primary_key=["category_key"],
        acquisition_tier=AcquisitionTier.LIVE,
        source_fingerprint=compute_fingerprint(cols, ["category_key"], []),
    )


def _rentals_schema() -> RelationSchema:
    cols = [
        ColumnInfo(name="rental_id", type="int", nullable=False),
        ColumnInfo(name="pickup_location_id", type="int", nullable=False),
        ColumnInfo(name="dropoff_location_id", type="int", nullable=True),
    ]
    fk = ForeignKey(
        columns=["pickup_location_id"],
        references=ForeignKeyRef(relation="analytics.dim_locations", columns=["location_id"]),
    )
    return RelationSchema(
        connection="warehouse_pg",
        relation="analytics.fct_rentals",
        kind="table",
        columns=cols,
        primary_key=["rental_id"],
        foreign_keys=[fk],
        acquisition_tier=AcquisitionTier.LIVE,
        source_fingerprint=compute_fingerprint(cols, ["rental_id"], [fk]),
    )


def _locations_schema() -> RelationSchema:
    cols = [
        ColumnInfo(name="location_id", type="int", nullable=False),
        ColumnInfo(name="city", type="string", nullable=True),
    ]
    return RelationSchema(
        connection="warehouse_pg",
        relation="analytics.dim_locations",
        kind="table",
        columns=cols,
        primary_key=["location_id"],
        acquisition_tier=AcquisitionTier.LIVE,
        source_fingerprint=compute_fingerprint(cols, ["location_id"], []),
    )


class TestSchemaJoinDrafting:
    async def test_valid_draft_is_applied_and_forces_review(self) -> None:
        class _StubDrafter(NullLLMDrafter):
            async def draft_schema_joins(
                self,
                schema: RelationSchema,
                candidate_columns: list[str],
                other_relations: dict[str, RelationSchema],
            ) -> list[JoinDraft]:
                if schema.relation != "analytics.fct_orders":
                    return []
                return [
                    JoinDraft(
                        column="category_key",
                        to="dim_categories",
                        to_column="category_key",
                        confidence=0.8,
                        reasoning="matches by name",
                    )
                ]

        result = await ContextBuilder(llm_drafter=_StubDrafter()).build(
            [_evidence(_orders_schema()), _evidence(_categories_schema())]
        )
        orders = next(p for p in result.proposals if p.content["name"] == "fct_orders")

        assert orders.content["joins"] == [
            {
                "to": "dim_categories",
                "on": "fct_orders.category_key = dim_categories.category_key",
                "relationship": Relationship.MANY_TO_ONE.value,
            }
        ]
        assert orders.content["meta"]["join_draft"] is True
        # Safety downgrade: a declared PK alone would give drafted_by=DETERMINISTIC,
        # confidence=1.0 — a guessed join must force this into review regardless.
        assert orders.drafted_by is DraftedBy.LLM
        assert orders.confidence < 1.0

    async def test_unknown_target_relation_is_discarded(self) -> None:
        class _StubDrafter(NullLLMDrafter):
            async def draft_schema_joins(
                self,
                schema: RelationSchema,
                candidate_columns: list[str],
                other_relations: dict[str, RelationSchema],
            ) -> list[JoinDraft]:
                return [
                    JoinDraft(
                        column="category_key", to="nonexistent", to_column="id", confidence=0.9
                    )
                ]

        result = await ContextBuilder(llm_drafter=_StubDrafter()).build(
            [_evidence(_orders_schema()), _evidence(_categories_schema())]
        )
        orders = next(p for p in result.proposals if p.content["name"] == "fct_orders")

        assert orders.content["joins"] == []
        assert orders.drafted_by is DraftedBy.DETERMINISTIC
        assert orders.confidence == 1.0

    async def test_unknown_target_column_is_discarded(self) -> None:
        class _StubDrafter(NullLLMDrafter):
            async def draft_schema_joins(
                self,
                schema: RelationSchema,
                candidate_columns: list[str],
                other_relations: dict[str, RelationSchema],
            ) -> list[JoinDraft]:
                return [
                    JoinDraft(
                        column="category_key",
                        to="dim_categories",
                        to_column="nonexistent_col",
                        confidence=0.9,
                    )
                ]

        result = await ContextBuilder(llm_drafter=_StubDrafter()).build(
            [_evidence(_orders_schema()), _evidence(_categories_schema())]
        )
        orders = next(p for p in result.proposals if p.content["name"] == "fct_orders")

        assert orders.content["joins"] == []
        assert orders.drafted_by is DraftedBy.DETERMINISTIC

    async def test_below_threshold_confidence_is_discarded(self) -> None:
        class _StubDrafter(NullLLMDrafter):
            async def draft_schema_joins(
                self,
                schema: RelationSchema,
                candidate_columns: list[str],
                other_relations: dict[str, RelationSchema],
            ) -> list[JoinDraft]:
                return [
                    JoinDraft(
                        column="category_key",
                        to="dim_categories",
                        to_column="category_key",
                        confidence=0.2,
                    )
                ]

        result = await ContextBuilder(llm_drafter=_StubDrafter()).build(
            [_evidence(_orders_schema()), _evidence(_categories_schema())]
        )
        orders = next(p for p in result.proposals if p.content["name"] == "fct_orders")

        assert orders.content["joins"] == []
        assert orders.drafted_by is DraftedBy.DETERMINISTIC

    async def test_confidence_is_capped_at_ceiling(self) -> None:
        class _StubDrafter(NullLLMDrafter):
            async def draft_schema_joins(
                self,
                schema: RelationSchema,
                candidate_columns: list[str],
                other_relations: dict[str, RelationSchema],
            ) -> list[JoinDraft]:
                if schema.relation != "analytics.fct_orders":
                    return []
                return [
                    JoinDraft(
                        column="category_key",
                        to="dim_categories",
                        to_column="category_key",
                        confidence=0.99,
                    )
                ]

        result = await ContextBuilder(llm_drafter=_StubDrafter()).build(
            [_evidence(_orders_schema()), _evidence(_categories_schema())]
        )
        orders = next(p for p in result.proposals if p.content["name"] == "fct_orders")

        assert orders.confidence == LLM_JOIN_CONFIDENCE_CEILING

    async def test_shared_target_with_declared_fk_gets_disambiguating_alias(self) -> None:
        class _StubDrafter(NullLLMDrafter):
            async def draft_schema_joins(
                self,
                schema: RelationSchema,
                candidate_columns: list[str],
                other_relations: dict[str, RelationSchema],
            ) -> list[JoinDraft]:
                if "dropoff_location_id" not in candidate_columns:
                    return []
                return [
                    JoinDraft(
                        column="dropoff_location_id",
                        to="dim_locations",
                        to_column="location_id",
                        confidence=0.8,
                    )
                ]

        result = await ContextBuilder(llm_drafter=_StubDrafter()).build(
            [_evidence(_rentals_schema()), _evidence(_locations_schema())]
        )
        rentals = next(p for p in result.proposals if p.content["name"] == "fct_rentals")

        assert len(rentals.content["joins"]) == 2
        assert {j["name"] for j in rentals.content["joins"]} == {
            "pickup_location",
            "dropoff_location",
        }

    async def test_single_relation_batch_skips_drafter_call(self) -> None:
        """No other relation in the batch to match against ⇒ draft_schema_joins is never invoked."""
        called = False

        class _StubDrafter(NullLLMDrafter):
            async def draft_schema_joins(
                self,
                schema: RelationSchema,
                candidate_columns: list[str],
                other_relations: dict[str, RelationSchema],
            ) -> list[JoinDraft]:
                nonlocal called
                called = True
                return []

        schema = _relation_schema(foreign_keys=[_customer_fk()])
        await ContextBuilder(llm_drafter=_StubDrafter()).build([_evidence(schema)])
        assert called is False

    async def test_null_llm_drafter_produces_no_drafted_joins(self) -> None:
        """Headless/default path: NullLLMDrafter is a no-op, matching today's behavior."""
        result = await ContextBuilder().build(
            [_evidence(_orders_schema()), _evidence(_categories_schema())]
        )
        orders = next(p for p in result.proposals if p.content["name"] == "fct_orders")

        assert orders.content["joins"] == []
        assert orders.drafted_by is DraftedBy.DETERMINISTIC


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
# _infer_measures / _infer_dimensions
# ---------------------------------------------------------------------------


class TestInferMeasures:
    def test_always_emits_row_count(self) -> None:
        measures = ContextBuilder._infer_measures([])
        assert len(measures) == 1
        assert measures[0]["name"] == "row_count"
        assert measures[0]["expr"] == "count(*)"
        assert measures[0]["additivity"] == "additive"

    def test_sums_numeric_non_id_columns(self) -> None:
        cols = [
            ColumnInfo(name="amount", type="decimal", nullable=True),
            ColumnInfo(name="price", type="float", nullable=True),
            ColumnInfo(name="qty", type="int", nullable=False),
        ]
        names = {m["name"] for m in ContextBuilder._infer_measures(cols)}
        assert "total_amount" in names
        assert "total_price" in names
        assert "total_qty" in names

    def test_skips_plain_id(self) -> None:
        cols = [ColumnInfo(name="id", type="int", nullable=False)]
        names = {m["name"] for m in ContextBuilder._infer_measures(cols)}
        assert "total_id" not in names
        assert "row_count" in names

    def test_skips_id_suffix_columns(self) -> None:
        cols = [
            ColumnInfo(name="customer_id", type="int", nullable=False),
            ColumnInfo(name="order_fk", type="int", nullable=False),
            ColumnInfo(name="account_key", type="int", nullable=False),
        ]
        names = {m["name"] for m in ContextBuilder._infer_measures(cols)}
        assert names == {"row_count"}

    def test_all_are_additive(self) -> None:
        cols = [ColumnInfo(name="revenue", type="float", nullable=True)]
        for m in ContextBuilder._infer_measures(cols):
            assert m["additivity"] == "additive"


class TestInferDimensions:
    def test_date_and_timestamp_become_dimensions(self) -> None:
        cols = [
            ColumnInfo(name="created_at", type="date", nullable=True),
            ColumnInfo(name="updated_at", type="timestamp", nullable=True),
        ]
        names = {d["name"] for d in ContextBuilder._infer_dimensions(cols)}
        assert names == {"created_at", "updated_at"}

    def test_bool_becomes_dimension(self) -> None:
        cols = [ColumnInfo(name="is_active", type="bool", nullable=False)]
        dims = ContextBuilder._infer_dimensions(cols)
        assert len(dims) == 1
        assert dims[0]["column"] == "is_active"

    def test_string_non_id_becomes_dimension(self) -> None:
        cols = [ColumnInfo(name="status", type="string", nullable=True)]
        dims = ContextBuilder._infer_dimensions(cols)
        assert len(dims) == 1
        assert dims[0]["name"] == "status"

    def test_skips_id_suffix_string_columns(self) -> None:
        cols = [
            ColumnInfo(name="customer_id", type="string", nullable=False),
            ColumnInfo(name="ref_fk", type="string", nullable=False),
        ]
        assert ContextBuilder._infer_dimensions(cols) == []

    def test_numeric_columns_produce_no_dimensions(self) -> None:
        cols = [
            ColumnInfo(name="amount", type="float", nullable=True),
            ColumnInfo(name="count", type="int", nullable=False),
        ]
        assert ContextBuilder._infer_dimensions(cols) == []


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

    async def test_draft_dimension_labels_is_empty(self) -> None:
        schema = _relation_schema(primary_key=[])
        dims = [{"name": "status", "column": "status"}]
        assert await NullLLMDrafter().draft_dimension_labels(schema, dims) == []

    async def test_draft_schema_joins_is_empty(self) -> None:
        schema = _relation_schema(primary_key=[])
        other = {"dim_customers": _relation_schema()}
        assert await NullLLMDrafter().draft_schema_joins(schema, ["customer_id"], other) == []
