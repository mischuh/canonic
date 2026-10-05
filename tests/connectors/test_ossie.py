"""Tests for the Apache Ossie connector (AMENDMENT-ossie-interchange §3, O1, O2)."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from canonic.config import CanonicConfig, Connection, ProjectConfig
from canonic.connectors.base import (
    AcquisitionTier,
    Capability,
    DefinitionEntityType,
    DocEvidence,
    UsageHint,
)
from canonic.connectors.factory import default_factory
from canonic.connectors.ossie import OssieConnector
from canonic.exc import ConnectionError, UnsupportedSourceVersionError

_UPSTREAM_TPCDS = Path(__file__).parent / "fixtures" / "ossie_upstream_tpcds.yaml"

_V011_DOCUMENT = """\
version: 0.1.1
semantic_model:
  - name: sales
    datasets:
      - name: orders
        source: db.public.orders
        primary_key: [order_id]
  - name: support
    datasets:
      - name: tickets
        source: db.public.tickets
"""


def _connector(path: Path) -> OssieConnector:
    return OssieConnector([str(path)], source="warehouse")


def _write(tmp_path: Path, text: str, name: str = "model.ossie.yaml") -> Path:
    path = tmp_path / name
    path.write_text(text)
    return path


class TestCapabilities:
    def test_declares_definitions_and_evidence(self, ossie_model_path: Path) -> None:
        caps = _connector(ossie_model_path).capabilities()
        assert Capability.EXTRACT_DEFINITIONS in caps
        assert Capability.EXTRACT_EVIDENCE in caps

    def test_declares_no_execution(self, ossie_model_path: Path) -> None:
        caps = _connector(ossie_model_path).capabilities()
        assert Capability.RUN_READ_ONLY_SQL not in caps
        assert Capability.INTROSPECT_SCHEMA not in caps


class TestTestConnection:
    async def test_ok_reports_files_models_and_version(self, ossie_model_path: Path) -> None:
        health = await _connector(ossie_model_path).test_connection()
        assert health.status == "ok"
        assert "1 file(s), 1 model(s), Ossie 0.2.0.dev0" in health.message

    async def test_query_sourced_dataset_is_a_warning(self, ossie_model_path: Path) -> None:
        health = await _connector(ossie_model_path).test_connection()
        assert any("shop/recent_orders" in w for w in health.warnings)

    async def test_error_on_missing_file(self, tmp_path: Path) -> None:
        health = await _connector(tmp_path / "missing.yaml").test_connection()
        assert health.status == "error"
        assert "not found" in health.message

    async def test_error_on_glob_without_matches(self, tmp_path: Path) -> None:
        connector = OssieConnector([str(tmp_path / "*.ossie.yaml")], source="warehouse")
        health = await connector.test_connection()
        assert health.status == "error"
        assert "no Ossie files match" in health.message

    async def test_error_on_url(self) -> None:
        connector = OssieConnector(["https://example.com/m.yaml"], source="warehouse")
        health = await connector.test_connection()
        assert health.status == "error"
        assert "not supported yet" in health.message

    async def test_error_on_invalid_yaml(self, tmp_path: Path) -> None:
        health = await _connector(_write(tmp_path, "version: [unclosed")).test_connection()
        assert health.status == "error"
        assert "cannot read Ossie file" in health.message

    async def test_error_on_invalid_model(self, tmp_path: Path) -> None:
        path = _write(tmp_path, "version: 0.2.0.dev0\nname: m\ndatasets: [{name: d}]\n")
        health = await _connector(path).test_connection()
        assert health.status == "error"
        assert "invalid Ossie semantic model" in health.message


class TestVersionPinning:
    @pytest.mark.parametrize(
        "document",
        [
            "version: 0.3.0\nname: m\ndatasets: [{name: d, source: t}]\n",
            "name: m\ndatasets: [{name: d, source: t}]\n",
            # Shape and version disagree: a flat document claiming 0.1.1 ...
            "version: 0.1.1\nname: m\ndatasets: [{name: d, source: t}]\n",
            # ... and a semantic_model array claiming 0.2.0.dev0.
            "version: 0.2.0.dev0\nsemantic_model: [{name: m, datasets: []}]\n",
        ],
        ids=["future", "missing", "flat-0.1.1", "array-0.2"],
    )
    async def test_unsupported_document_fails_test_connection(
        self, tmp_path: Path, document: str
    ) -> None:
        health = await _connector(_write(tmp_path, document)).test_connection()
        assert health.status == "error"
        assert "unsupported" in health.message

    async def test_extract_raises_and_ingests_nothing(
        self, tmp_path: Path, ossie_model_path: Path
    ) -> None:
        bad = _write(tmp_path, "version: 0.3.0\nname: m\ndatasets: [{name: d, source: t}]\n")
        connector = OssieConnector([str(ossie_model_path), str(bad)], source="warehouse")
        with pytest.raises(UnsupportedSourceVersionError):
            await connector.extract_definitions()
        with pytest.raises(UnsupportedSourceVersionError):
            await connector.extract_evidence()

    async def test_v011_array_shape_yields_every_model(self, tmp_path: Path) -> None:
        connector = _connector(_write(tmp_path, _V011_DOCUMENT))
        health = await connector.test_connection()
        assert health.status == "ok"
        assert "2 model(s), Ossie 0.1.1" in health.message
        extract = await connector.extract_definitions()
        refs = {d.native_ref for d in extract.definitions}
        assert refs == {"ossie:sales/orders", "ossie:support/tickets"}

    async def test_upstream_tpcds_example_parses(self) -> None:
        connector = _connector(_UPSTREAM_TPCDS)
        assert (await connector.test_connection()).status == "ok"
        extract = await connector.extract_definitions()
        models = [d for d in extract.definitions if d.entity_type == DefinitionEntityType.MODEL]
        assert {d.entity for d in models} >= {"tpcds.public.store_sales", "tpcds.public.customer"}


class TestExtractDefinitions:
    async def test_dataset_becomes_model_and_entity(self, ossie_model_path: Path) -> None:
        extract = await _connector(ossie_model_path).extract_definitions()
        by_key = {(d.entity_type, d.entity): d for d in extract.definitions}

        model = by_key[(DefinitionEntityType.MODEL, "main.orders")]
        assert model.description == "One row per order"
        assert model.grain == ["order_id"]
        assert model.native_ref == "ossie:shop/orders"

        entity = by_key[(DefinitionEntityType.ENTITY, "orders")]
        assert entity.references == ["main.orders"]
        assert entity.grain == ["order_id"]

    async def test_every_item_is_modeling_tier_on_the_target(self, ossie_model_path: Path) -> None:
        extract = await _connector(ossie_model_path).extract_definitions()
        assert extract.relations == []
        assert extract.definitions
        for definition in extract.definitions:
            assert definition.source == "warehouse"
            assert definition.acquisition_tier == AcquisitionTier.MODELING

    async def test_query_sourced_dataset_is_warned_not_mapped(
        self, ossie_model_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.WARNING, logger="canonic.connectors.ossie"):
            extract = await _connector(ossie_model_path).extract_definitions()
        assert all("recent_orders" not in d.native_ref for d in extract.definitions)
        assert "shop/recent_orders is query-sourced" in caplog.text

    async def test_extraction_is_deterministic(self, ossie_model_path: Path) -> None:
        first = await _connector(ossie_model_path).extract_definitions()
        second = await _connector(ossie_model_path).extract_definitions()
        assert first == second


class TestExtractEvidence:
    async def _docs(self, path: Path) -> dict[str, DocEvidence]:
        items = await _connector(path).extract_evidence()
        docs = [i for i in items if isinstance(i, DocEvidence)]
        assert len(docs) == len(items)
        return {d.native_ref: d for d in docs}

    async def test_instructions_on_every_object_kind(self, ossie_model_path: Path) -> None:
        docs = await self._docs(ossie_model_path)
        assert set(docs) == {
            "ossie:shop",
            "ossie:shop/orders",
            "ossie:shop/orders/status",
            "ossie:shop#metric/revenue",
            "ossie:shop#relationship/orders_to_customers",
        }

    async def test_topic_refs_are_qualified_by_target_and_relation(
        self, ossie_model_path: Path
    ) -> None:
        docs = await self._docs(ossie_model_path)
        assert docs["ossie:shop"].topic_refs == []
        assert docs["ossie:shop/orders"].topic_refs == ["warehouse.orders"]
        assert docs["ossie:shop/orders/status"].topic_refs == ["warehouse.orders.status"]
        assert docs["ossie:shop#metric/revenue"].topic_refs == ["revenue"]
        relationship = docs["ossie:shop#relationship/orders_to_customers"]
        assert relationship.topic_refs == ["warehouse.orders", "warehouse.customers"]

    async def test_string_ai_context_is_instructions(self, ossie_model_path: Path) -> None:
        docs = await self._docs(ossie_model_path)
        relationship = docs["ossie:shop#relationship/orders_to_customers"]
        assert relationship.body == "Every order belongs to exactly one customer."

    async def test_default_usage_hint_is_reference(self, ossie_model_path: Path) -> None:
        docs = await self._docs(ossie_model_path)
        assert {d.usage_hint for d in docs.values()} == {UsageHint.REFERENCE}

    async def test_fingerprints_are_stable(self, ossie_model_path: Path) -> None:
        first = await self._docs(ossie_model_path)
        second = await self._docs(ossie_model_path)
        assert {k: d.source_fingerprint for k, d in first.items()} == {
            k: d.source_fingerprint for k, d in second.items()
        }


class TestFactory:
    def test_resolves_through_default_factory(self, ossie_model_path: Path) -> None:
        conn = Connection(
            id="shop_ossie",
            type="ossie",
            params={"paths": [str(ossie_model_path)], "target_connection": "warehouse"},
        )
        connector = default_factory.create(conn)
        assert isinstance(connector, OssieConnector)

    def test_single_string_path_is_accepted(self, ossie_model_path: Path) -> None:
        conn = Connection(
            id="shop_ossie",
            type="ossie",
            params={"paths": str(ossie_model_path), "target_connection": "warehouse"},
        )
        assert isinstance(default_factory.create(conn), OssieConnector)

    def test_missing_paths_is_a_connection_error(self) -> None:
        conn = Connection(id="shop_ossie", type="ossie", params={"target_connection": "w"})
        with pytest.raises(ConnectionError, match=r"requires params\.paths"):
            default_factory.create(conn)

    async def test_evidence_is_stamped_with_target_connection(self, ossie_model_path: Path) -> None:
        conn = Connection(
            id="shop_ossie",
            type="ossie",
            params={"paths": [str(ossie_model_path)], "target_connection": "warehouse"},
        )
        connector = default_factory.create(conn)
        assert isinstance(connector, OssieConnector)
        extract = await connector.extract_definitions()
        assert {d.source for d in extract.definitions} == {"warehouse"}


class TestConfigValidation:
    @staticmethod
    def _config(*connections: Connection) -> CanonicConfig:
        return CanonicConfig(
            version=1, project=ProjectConfig(name="p"), connections=list(connections)
        )

    def test_valid_primary_target(self) -> None:
        self._config(
            Connection(id="warehouse", type="sqlite", params={"path": "x.db"}),
            Connection(
                id="shop_ossie",
                type="ossie",
                params={"paths": ["m.yaml"], "target_connection": "warehouse"},
            ),
        )

    def test_missing_target_connection_fails(self) -> None:
        with pytest.raises(ValueError, match="requires params.target_connection"):
            self._config(
                Connection(id="warehouse", type="sqlite", params={"path": "x.db"}),
                Connection(id="shop_ossie", type="ossie", params={"paths": ["m.yaml"]}),
            )

    def test_non_primary_target_fails(self) -> None:
        with pytest.raises(ValueError, match="must be a primary, queryable connection"):
            self._config(
                Connection(id="manifest", type="dbt", params={"manifest_path": "m.json"}),
                Connection(
                    id="shop_ossie",
                    type="ossie",
                    params={"paths": ["m.yaml"], "target_connection": "manifest"},
                ),
            )

    def test_self_target_fails(self) -> None:
        with pytest.raises(ValueError, match="must be a primary, queryable connection"):
            self._config(
                Connection(
                    id="shop_ossie",
                    type="ossie",
                    params={"paths": ["m.yaml"], "target_connection": "shop_ossie"},
                ),
            )
