"""Tests for the dbt manifest connector (SPEC-E3 §4, S1 AC1/AC2)."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from pathlib import Path

from canon.connectors.base import (
    AcquisitionTier,
    Capability,
    DefinitionEntityType,
    DefinitionEvidence,
    DefinitionExtract,
    RelationSchema,
)
from canon.connectors.dbt import DbtConnector, _normalize_type
from canon.exc import ConnectionError, UnsupportedSourceVersionError
from canon.ingestion.models import EvidenceKind
from canon.ingestion.source import evidence_from_definitions
from canon.semantic.models import Additivity, Relationship


class TestCapabilities:
    def test_declares_extract_definitions(self, dbt_manifest_path: Path) -> None:
        connector = DbtConnector(dbt_manifest_path)
        caps = connector.capabilities()
        assert Capability.EXTRACT_DEFINITIONS in caps

    def test_does_not_declare_introspect_schema(self, dbt_manifest_path: Path) -> None:
        connector = DbtConnector(dbt_manifest_path)
        assert Capability.INTROSPECT_SCHEMA not in connector.capabilities()


class TestTestConnection:
    async def test_ok_with_valid_manifest(self, dbt_manifest_path: Path) -> None:
        connector = DbtConnector(dbt_manifest_path)
        health = await connector.test_connection()
        assert health.status == "ok"
        assert health.message is not None
        assert "1.7.0" in health.message
        assert "v11" in health.message

    async def test_error_on_missing_file(self, tmp_path: Path) -> None:
        connector = DbtConnector(tmp_path / "missing.json")
        health = await connector.test_connection()
        assert health.status == "error"
        assert "not found" in (health.message or "")

    async def test_error_on_invalid_json(self, tmp_path: Path) -> None:
        bad = tmp_path / "bad.json"
        bad.write_text("not json")
        connector = DbtConnector(bad)
        health = await connector.test_connection()
        assert health.status == "error"


def _write_manifest(path: Path, schema_version: str, *, dbt_version: str = "1.5.0") -> Path:
    """Write a minimal manifest pinned to a given schema version (e.g. ``v9``)."""
    import json

    path.write_text(
        json.dumps(
            {
                "metadata": {
                    "dbt_schema_version": (
                        f"https://schemas.getdbt.com/dbt/manifest/{schema_version}.json"
                    ),
                    "dbt_version": dbt_version,
                },
                "nodes": {},
            }
        )
    )
    return path


class TestVersionPinning:
    """SPEC-E3 §6, S5 — out-of-range manifest fails loudly, ingests nothing (AC1)."""

    async def test_connection_error_on_below_floor_manifest(self, tmp_path: Path) -> None:
        manifest = _write_manifest(tmp_path / "old.json", "v9")
        connector = DbtConnector(manifest)
        health = await connector.test_connection()
        assert health.status == "error"
        assert "v9" in (health.message or "")
        assert "v10" in (health.message or "")

    async def test_extract_raises_on_below_floor_manifest(self, tmp_path: Path) -> None:
        manifest = _write_manifest(tmp_path / "old.json", "v9")
        connector = DbtConnector(manifest)
        with pytest.raises(UnsupportedSourceVersionError) as excinfo:
            await connector.extract_definitions()
        exc = excinfo.value
        assert exc.detected == "v9"
        assert "v10" in exc.supported
        assert exc.exit_code == 13
        assert isinstance(exc, ConnectionError)

    async def test_unknown_schema_version_is_rejected(self, tmp_path: Path) -> None:
        import json

        manifest = tmp_path / "no_version.json"
        manifest.write_text(json.dumps({"metadata": {"dbt_version": "1.5.0"}, "nodes": {}}))
        connector = DbtConnector(manifest)
        with pytest.raises(UnsupportedSourceVersionError):
            await connector.extract_definitions()

    async def test_supported_manifest_extracts(self, dbt_manifest_path: Path) -> None:
        connector = DbtConnector(dbt_manifest_path)
        result = await connector.extract_definitions()
        assert isinstance(result, DefinitionExtract)


class TestTypeMapping:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("bigint", "int"),
            ("integer", "int"),
            ("numeric", "decimal"),
            ("double precision", "float"),
            ("boolean", "bool"),
            ("text", "string"),
            ("varchar", "string"),
            ("date", "date"),
            ("timestamp", "timestamp"),
            ("timestamptz", "timestamp"),
            ("jsonb", "json"),
            ("variant", "json"),
            ("numeric(12,2)", "decimal"),
            ("varchar(255)", "string"),
        ],
    )
    def test_known_types(self, raw: str, expected: str) -> None:
        assert _normalize_type(raw, "analytics.t", "c") == expected

    def test_unmapped_type_falls_back_to_json_with_warning(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.WARNING):
            result = _normalize_type("geometry", "analytics.t", "geom")
        assert result == "json"
        assert "geometry" in caplog.text

    def test_missing_type_falls_back_to_json_with_warning(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.WARNING):
            result = _normalize_type(None, "analytics.t", "col")
        assert result == "json"
        assert "missing" in caplog.text.lower()


class TestExtractDefinitions:
    async def test_returns_definition_extract(self, dbt_manifest_path: Path) -> None:
        connector = DbtConnector(dbt_manifest_path)
        result = await connector.extract_definitions()
        assert isinstance(result, DefinitionExtract)

    async def test_ac1_models_yield_relation_schema_at_modeling_tier(
        self, dbt_manifest_path: Path
    ) -> None:
        connector = DbtConnector(dbt_manifest_path)
        result = await connector.extract_definitions()
        relations = {r.relation: r for r in result.relations}
        assert "analytics.fct_orders" in relations
        orders = relations["analytics.fct_orders"]
        assert orders.acquisition_tier == AcquisitionTier.MODELING
        assert orders.connection == "dbt"
        col_types = {c.name: c.type for c in orders.columns}
        assert col_types["order_id"] == "int"
        assert col_types["amount"] == "decimal"
        assert col_types["order_date"] == "date"

    async def test_ac1_primary_key_propagated(self, dbt_manifest_path: Path) -> None:
        connector = DbtConnector(dbt_manifest_path)
        result = await connector.extract_definitions()
        orders = next(r for r in result.relations if "fct_orders" in r.relation)
        assert orders.primary_key == ["order_id"]

    async def test_ac1_primary_key_from_column_constraint(self, dbt_manifest_path: Path) -> None:
        connector = DbtConnector(dbt_manifest_path)
        result = await connector.extract_definitions()
        customers = next(r for r in result.relations if "dim_customers" in r.relation)
        assert customers.primary_key == ["customer_id"]

    async def test_ac1_no_test_nodes_in_relations(self, dbt_manifest_path: Path) -> None:
        connector = DbtConnector(dbt_manifest_path)
        result = await connector.extract_definitions()
        for rel in result.relations:
            assert "not_null" not in rel.relation

    async def test_ac1_measure_has_additivity_and_references(self, dbt_manifest_path: Path) -> None:
        connector = DbtConnector(dbt_manifest_path)
        result = await connector.extract_definitions()
        measures = [d for d in result.definitions if d.entity_type == DefinitionEntityType.MEASURE]
        revenue = next((m for m in measures if m.entity == "total_revenue"), None)
        assert revenue is not None
        assert revenue.additivity == Additivity.ADDITIVE
        assert revenue.expr == "sum(amount)"
        assert len(revenue.references) > 0

    async def test_ac1_count_distinct_is_additive(self, dbt_manifest_path: Path) -> None:
        connector = DbtConnector(dbt_manifest_path)
        result = await connector.extract_definitions()
        measures = {
            d.entity: d for d in result.definitions if d.entity_type == DefinitionEntityType.MEASURE
        }
        assert measures["unique_customers"].additivity == Additivity.ADDITIVE

    async def test_ac1_average_is_non_additive(self, dbt_manifest_path: Path) -> None:
        connector = DbtConnector(dbt_manifest_path)
        result = await connector.extract_definitions()
        measures = {
            d.entity: d for d in result.definitions if d.entity_type == DefinitionEntityType.MEASURE
        }
        assert measures["avg_order_value"].additivity == Additivity.NON_ADDITIVE

    async def test_ac1_non_additive_dimension_yields_semi_additive(
        self, dbt_manifest_path: Path
    ) -> None:
        connector = DbtConnector(dbt_manifest_path)
        result = await connector.extract_definitions()
        measures = {
            d.entity: d for d in result.definitions if d.entity_type == DefinitionEntityType.MEASURE
        }
        assert measures["balance"].additivity == Additivity.SEMI_ADDITIVE

    async def test_ac1_join_has_relationship(self, dbt_manifest_path: Path) -> None:
        connector = DbtConnector(dbt_manifest_path)
        result = await connector.extract_definitions()
        joins = [d for d in result.definitions if d.entity_type == DefinitionEntityType.JOIN]
        assert len(joins) >= 1
        for join in joins:
            assert len(join.joins) >= 1
            assert join.joins[0].relationship in Relationship

    async def test_ac1_native_ref_is_dbt_unique_id(self, dbt_manifest_path: Path) -> None:
        connector = DbtConnector(dbt_manifest_path)
        result = await connector.extract_definitions()
        model_defs = [d for d in result.definitions if d.entity_type == DefinitionEntityType.MODEL]
        orders_def = next(d for d in model_defs if "fct_orders" in d.entity)
        assert orders_def.native_ref == "model.analytics.fct_orders"

    async def test_ac1_no_dbt_specific_keys_in_definition(self, dbt_manifest_path: Path) -> None:
        connector = DbtConnector(dbt_manifest_path)
        result = await connector.extract_definitions()
        dbt_keys = {"unique_id", "resource_type", "alias", "config", "fqn"}
        for defn in result.definitions:
            dumped = set(defn.model_dump(mode="json").keys())
            assert not dumped & dbt_keys, (
                f"dbt-specific keys leaked into {defn.entity}: {dumped & dbt_keys}"
            )

    async def test_ac1_simple_metric_not_duplicated(self, dbt_manifest_path: Path) -> None:
        # A MetricFlow "simple" metric is a reference to a semantic-model measure, not a
        # new measure definition. The semantic model already emits MEASURE evidence for
        # "total_revenue"; the "revenue" metric should NOT produce a second MEASURE entry.
        connector = DbtConnector(dbt_manifest_path)
        result = await connector.extract_definitions()
        measures = [d for d in result.definitions if d.entity_type == DefinitionEntityType.MEASURE]
        measure_names = {m.entity for m in measures}
        assert "total_revenue" in measure_names  # semantic model measure is present
        assert "revenue" not in measure_names  # metric reference does not add a duplicate

    async def test_ac1_entity_evidence_has_references_to_backing_relation(
        self, dbt_manifest_path: Path
    ) -> None:
        # ENTITY evidence must carry references=[node_relation] so the builder can
        # correlate the grain to the right DuckDB RelationSchema during bootstrap.
        connector = DbtConnector(dbt_manifest_path)
        result = await connector.extract_definitions()
        entities = [d for d in result.definitions if d.entity_type == DefinitionEntityType.ENTITY]
        orders_entity = next((e for e in entities if e.entity == "orders"), None)
        assert orders_entity is not None
        assert "analytics.fct_orders" in orders_entity.references

    async def test_ac2_unmappable_type_recorded_with_warning(
        self, dbt_manifest_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        connector = DbtConnector(dbt_manifest_path)
        with caplog.at_level(logging.WARNING):
            result = await connector.extract_definitions()
        assert "geometry" in caplog.text
        orders = next(r for r in result.relations if "fct_orders" in r.relation)
        geom_col = next(c for c in orders.columns if c.name == "geom")
        assert geom_col.type == "json"

    async def test_ac2_unknown_agg_skips_with_warning(
        self, dbt_manifest_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        connector = DbtConnector(dbt_manifest_path)
        with caplog.at_level(logging.WARNING):
            result = await connector.extract_definitions()
        assert "hyperloglog" in caplog.text
        active = next((d for d in result.definitions if d.entity == "active_customers"), None)
        assert active is not None
        assert active.additivity is None

    async def test_ac2_composite_metric_type_skipped_without_warning(
        self, dbt_manifest_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        # MetricFlow composite types (ratio, derived, …) are recognised but not yet
        # implemented; they are skipped at DEBUG level — no WARNING noise for the user.
        connector = DbtConnector(dbt_manifest_path)
        with caplog.at_level(logging.WARNING):
            result = await connector.extract_definitions()
        assert "ratio" not in caplog.text
        metric_names = {
            d.entity for d in result.definitions if d.entity_type == DefinitionEntityType.MEASURE
        }
        assert "revenue_ratio" not in metric_names

    async def test_ac2_extraction_does_not_raise_on_unmappable(
        self, dbt_manifest_path: Path
    ) -> None:
        connector = DbtConnector(dbt_manifest_path)
        result = await connector.extract_definitions()
        assert len(result.relations) > 0

    async def test_definition_evidence_round_trips(self, dbt_manifest_path: Path) -> None:
        connector = DbtConnector(dbt_manifest_path)
        result = await connector.extract_definitions()
        for defn in result.definitions:
            round_tripped = DefinitionEvidence.model_validate(defn.model_dump(mode="json"))
            assert round_tripped == defn

    async def test_relation_schema_round_trips(self, dbt_manifest_path: Path) -> None:
        connector = DbtConnector(dbt_manifest_path)
        result = await connector.extract_definitions()
        for rel in result.relations:
            round_tripped = RelationSchema.model_validate(rel.model_dump(mode="json"))
            assert round_tripped == rel


class TestEvidenceFromDefinitionsSeam:
    async def test_emits_relation_schema_and_definition_items(
        self, dbt_manifest_path: Path
    ) -> None:
        connector = DbtConnector(dbt_manifest_path)
        items = await evidence_from_definitions(connector, "dbt_prod")
        kinds = {item.kind for item in items}
        assert EvidenceKind.RELATION_SCHEMA in kinds
        assert EvidenceKind.DEFINITION in kinds

    async def test_all_items_at_modeling_tier(self, dbt_manifest_path: Path) -> None:
        connector = DbtConnector(dbt_manifest_path)
        items = await evidence_from_definitions(connector, "dbt_prod")
        for item in items:
            assert item.acquisition_tier == AcquisitionTier.MODELING

    async def test_source_stamped_correctly(self, dbt_manifest_path: Path) -> None:
        connector = DbtConnector(dbt_manifest_path)
        items = await evidence_from_definitions(connector, "dbt_prod")
        for item in items:
            assert item.source == "dbt_prod"

    async def test_no_vendor_keys_in_payloads(self, dbt_manifest_path: Path) -> None:
        connector = DbtConnector(dbt_manifest_path)
        items = await evidence_from_definitions(connector, "dbt_prod")
        dbt_keys = {"unique_id", "resource_type", "alias", "config", "fqn"}
        for item in items:
            assert not set(item.payload.keys()) & dbt_keys, (
                f"dbt-specific keys in {item.kind} payload: {set(item.payload.keys()) & dbt_keys}"
            )
