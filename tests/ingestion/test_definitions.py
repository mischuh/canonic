"""Modeling-tier definitions folded into relation drafts (dbt, Ossie)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from canonic.connectors.base import (
    AcquisitionTier,
    ColumnInfo,
    DefinitionEntityType,
    DefinitionEvidence,
    ForeignKey,
    ForeignKeyRef,
    JoinSpec,
    RelationSchema,
    ReviewFlag,
    compute_fingerprint,
)
from canonic.ingestion.builder import (
    MODELING_REVIEW_CONFIDENCE,
    ContextBuilder,
    JoinDraft,
    NullLLMDrafter,
)
from canonic.ingestion.definitions import DefinitionIndex
from canonic.ingestion.models import DraftedBy, EvidenceItem, EvidenceKind
from canonic.semantic.models import Additivity, Relationship

_NOW = datetime(2026, 10, 5, 12, 0, 0, tzinfo=UTC)
_CONN = "warehouse"


def _schema(
    relation: str, columns: dict[str, str], *, fks: list[ForeignKey] | None = None
) -> RelationSchema:
    cols = [ColumnInfo(name=n, type=t, nullable=True) for n, t in columns.items()]  # type: ignore[arg-type]
    pk = [next(iter(columns))]
    return RelationSchema(
        connection=_CONN,
        relation=relation,
        kind="table",
        columns=cols,
        primary_key=pk,
        foreign_keys=fks or [],
        acquisition_tier=AcquisitionTier.LIVE,
        source_fingerprint=compute_fingerprint(cols, pk, fks or []),
    )


_ORDERS = {
    "order_id": "int",
    "customer_id": "int",
    "status": "string",
    "order_date": "date",
    "amount": "decimal",
}
_CUSTOMERS = {"customer_id": "int", "country": "string"}


def _relation_item(schema: RelationSchema) -> EvidenceItem:
    return EvidenceItem(
        source=_CONN,
        kind=EvidenceKind.RELATION_SCHEMA,
        acquisition_tier=AcquisitionTier.LIVE,
        payload=schema.model_dump(mode="json"),
        source_fingerprint=schema.source_fingerprint or "sha256:none",
        observed_at=_NOW,
    )


def _definition(**fields: Any) -> EvidenceItem:
    fields.setdefault("native_ref", f"test#{fields['entity']}")
    definition = DefinitionEvidence(
        source=_CONN, acquisition_tier=AcquisitionTier.MODELING, **fields
    )
    return EvidenceItem(
        source=_CONN,
        kind=EvidenceKind.DEFINITION,
        acquisition_tier=AcquisitionTier.MODELING,
        payload=definition.model_dump(mode="json"),
        source_fingerprint=f"sha256:{fields['entity']}",
        observed_at=_NOW,
    )


def _dimension(name: str, **fields: Any) -> EvidenceItem:
    return _definition(
        entity=name,
        entity_type=DefinitionEntityType.DIMENSION,
        references=["main.orders"],
        **fields,
    )


def _measure(name: str, expr: str, additivity: Additivity | None, **fields: Any) -> EvidenceItem:
    fields.setdefault("references", ["main.orders"])
    return _definition(
        entity=name,
        entity_type=DefinitionEntityType.MEASURE,
        expr=expr,
        additivity=additivity,
        **fields,
    )


def _join(
    name: str, on: str, *, relationship: Relationship = Relationship.MANY_TO_ONE, **fields: Any
) -> EvidenceItem:
    return _definition(
        entity=name,
        entity_type=DefinitionEntityType.JOIN,
        references=["main.orders", "main.customers"],
        joins=[JoinSpec(left="orders", right="customers", relationship=relationship, on=on)],
        **fields,
    )


async def _build(*items: EvidenceItem, drafter: Any = None) -> tuple[dict[str, Any], Any]:
    evidence = [
        _relation_item(_schema("main.orders", _ORDERS)),
        _relation_item(_schema("main.customers", _CUSTOMERS)),
        *items,
    ]
    result = await ContextBuilder(drafter).build(evidence)
    proposal = next(p for p in result.proposals if p.target.endswith("/orders.yaml"))
    return {"proposal": proposal, "content": proposal.content}, result


class TestDimensions:
    async def test_column_dimension_replaces_the_inferred_one(self) -> None:
        built, _ = await _build(
            _dimension("status", column="status", description="Order status", aliases=["state"])
        )
        dims = [d for d in built["content"]["dimensions"] if d.get("column") == "status"]
        assert dims == [
            {
                "name": "status",
                "column": "status",
                "description": "Order status",
                "aliases": ["state"],
            }
        ]

    async def test_renamed_column_dimension_drops_the_inferred_duplicate(self) -> None:
        built, _ = await _build(_dimension("order_status", column="status"))
        names = [d["name"] for d in built["content"]["dimensions"]]
        assert "order_status" in names
        assert "status" not in names

    async def test_bare_column_expr_from_dbt_becomes_a_column(self) -> None:
        built, _ = await _build(_dimension("status", expr="status"))
        (dim,) = [d for d in built["content"]["dimensions"] if d["name"] == "status"]
        assert dim == {"name": "status", "column": "status"}

    async def test_expr_dimension_gets_an_inferred_type(self) -> None:
        built, _ = await _build(
            _dimension("status_upper", expr="UPPER(status)"),
            _dimension("order_year", expr="EXTRACT(year FROM order_date)"),
        )
        dims = {d["name"]: d for d in built["content"]["dimensions"]}
        assert dims["status_upper"]["type"] == "string"
        assert dims["order_year"]["type"] == "int"

    async def test_untypeable_expr_is_skipped(self) -> None:
        built, result = await _build(_dimension("odd", expr="my_udf(status)"))
        assert "odd" not in {d["name"] for d in built["content"]["dimensions"]}
        (skip,) = result.skipped
        assert "type cannot be inferred" in skip.reason

    async def test_missing_column_is_skipped(self) -> None:
        _, result = await _build(_dimension("region", column="region"))
        (skip,) = result.skipped
        assert "column 'region' is not on 'orders'" in skip.reason

    async def test_llm_aliases_never_replace_modeling_aliases(self) -> None:
        class _AliasDrafter(NullLLMDrafter):
            async def draft_dimension_labels(self, schema, dimensions):  # type: ignore[no-untyped-def]
                from canonic.ingestion.builder import DimensionEnrichment

                return [
                    DimensionEnrichment(
                        name=d["name"], label="Guess", aliases=["guess"], confidence=1.0
                    )
                    for d in dimensions
                ]

        built, _ = await _build(
            _dimension("status", column="status", aliases=["state"]), drafter=_AliasDrafter()
        )
        (dim,) = [d for d in built["content"]["dimensions"] if d["name"] == "status"]
        assert dim["aliases"] == ["state"]
        assert dim["label"] == "Guess"


class TestMeasures:
    async def test_identical_definitions_from_two_sources_merge(self) -> None:
        built, result = await _build(
            _measure("revenue", "SUM(amount)", Additivity.ADDITIVE, native_ref="dbt#revenue"),
            _measure("revenue", "SUM(amount)", Additivity.ADDITIVE, native_ref="ossie#revenue"),
        )
        assert [m["name"] for m in built["content"]["measures"]] == ["revenue"]
        assert result.skipped == []
        assert "review_flags" not in built["content"]["meta"]

    async def test_conflicting_definitions_keep_the_first_and_go_to_review(self) -> None:
        built, result = await _build(
            _measure("revenue", "SUM(amount)", Additivity.ADDITIVE, native_ref="dbt#revenue"),
            _measure("revenue", "SUM(amount * 2)", Additivity.ADDITIVE, native_ref="ossie#revenue"),
        )
        assert built["content"]["measures"] == [
            {"name": "revenue", "expr": "SUM(amount)", "additivity": "additive"}
        ]
        (skip,) = result.skipped
        assert "ossie#revenue" in skip.reason
        assert "conflicts with an earlier definition" in skip.reason
        assert built["content"]["meta"]["review_flags"] == [
            "measure revenue: conflicting definitions"
        ]
        assert built["proposal"].confidence == MODELING_REVIEW_CONFIDENCE

    async def test_flagged_measure_caps_confidence(self) -> None:
        built, _ = await _build(
            _measure(
                "aov",
                "AVG(amount)",
                Additivity.NON_ADDITIVE,
                review_flags=[ReviewFlag.AVG_SUGGESTS_RATIO],
            )
        )
        assert built["content"]["meta"]["review_flags"] == ["measure aov: avg_suggests_ratio"]
        assert built["proposal"].confidence == MODELING_REVIEW_CONFIDENCE
        assert built["proposal"].drafted_by == DraftedBy.DETERMINISTIC

    async def test_measure_on_an_unintrospected_relation_is_reported(self) -> None:
        _, result = await _build(
            _measure("tickets", "COUNT(id)", Additivity.ADDITIVE, references=["main.tickets"])
        )
        (skip,) = result.skipped
        assert "no introspected relation 'tickets' in this run" in skip.reason


class TestJoins:
    async def test_modeling_join_is_drafted_with_its_cardinality(self) -> None:
        built, _ = await _build(
            _join(
                "orders_to_customers",
                "orders.customer_id = customers.customer_id",
                relationship=Relationship.ONE_TO_ONE,
            )
        )
        assert built["content"]["joins"] == [
            {
                "to": "customers",
                "on": "orders.customer_id = customers.customer_id",
                "relationship": "one_to_one",
            }
        ]

    async def test_fk_join_with_the_same_predicate_is_replaced(self) -> None:
        fk = ForeignKey(
            columns=["customer_id"],
            references=ForeignKeyRef(relation="main.customers", columns=["customer_id"]),
        )
        evidence = [
            _relation_item(_schema("main.orders", _ORDERS, fks=[fk])),
            _relation_item(_schema("main.customers", _CUSTOMERS)),
            _join("o2c", "orders.customer_id = customers.customer_id"),
        ]
        result = await ContextBuilder().build(evidence)
        orders = next(p for p in result.proposals if p.target.endswith("/orders.yaml"))
        assert len(orders.content["joins"]) == 1

    async def test_two_joins_to_one_target_get_aliases(self) -> None:
        built, _ = await _build(
            _join("by_customer", "orders.customer_id = customers.customer_id"),
            _join("by_country", "orders.status = customers.country"),
        )
        assert sorted(j["name"] for j in built["content"]["joins"]) == ["customer", "status"]

    async def test_covered_columns_get_no_llm_join_guess(self) -> None:
        offered: list[list[str]] = []

        class _RecordingDrafter(NullLLMDrafter):
            async def draft_schema_joins(self, schema, candidate_columns, other_relations):  # type: ignore[no-untyped-def]
                if schema.relation == "main.orders":
                    offered.append(list(candidate_columns))
                return [
                    JoinDraft(column=c, to="customers", to_column="customer_id", confidence=0.8)
                    for c in candidate_columns
                ]

        built, _ = await _build(
            _join("o2c", "orders.customer_id = customers.customer_id"), drafter=_RecordingDrafter()
        )
        assert offered
        assert all("customer_id" not in cols for cols in offered)
        joins_on_customer_id = [
            j for j in built["content"]["joins"] if j["on"].startswith("orders.customer_id")
        ]
        assert len(joins_on_customer_id) == 1

    async def test_join_column_missing_on_a_side_is_skipped(self) -> None:
        built, result = await _build(_join("bad", "orders.customer_id = customers.id"))
        assert built["content"]["joins"] == []
        (skip,) = result.skipped
        assert "join predicate column 'customers.id' does not exist" in skip.reason

    async def test_flagged_join_caps_confidence(self) -> None:
        built, _ = await _build(
            _join(
                "by_country",
                "orders.status = customers.country",
                review_flags=[ReviewFlag.JOIN_COLUMNS_NOT_A_KEY],
            )
        )
        assert built["content"]["meta"]["review_flags"] == [
            "join by_country: join_columns_not_a_key"
        ]
        assert built["proposal"].confidence == MODELING_REVIEW_CONFIDENCE

    async def test_join_without_predicate_is_ignored(self) -> None:
        """dbt joins state no predicate, they stay out of drafts as before."""
        item = _definition(
            entity="customer",
            entity_type=DefinitionEntityType.JOIN,
            joins=[
                JoinSpec(
                    left="orders.customer", right="customer", relationship=Relationship.MANY_TO_ONE
                )
            ],
        )
        built, result = await _build(item)
        assert built["content"]["joins"] == []
        assert result.skipped == []


class TestIndex:
    def test_invalid_payload_is_reported_not_raised(self) -> None:
        index = DefinitionIndex()
        index.add(
            EvidenceItem(
                source=_CONN,
                kind=EvidenceKind.DEFINITION,
                acquisition_tier=AcquisitionTier.MODELING,
                payload={"entity": "x"},
                source_fingerprint="sha256:x",
                observed_at=_NOW,
            )
        )
        (unplaced,) = index.resolve({})
        assert unplaced.reason.startswith("invalid definition payload")

    def test_non_modeling_items_are_ignored(self) -> None:
        index = DefinitionIndex()
        index.add(_relation_item(_schema("main.orders", _ORDERS)))
        assert index.resolve({}) == []
