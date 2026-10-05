"""Tests for the Apache Ossie connector (AMENDMENT-ossie-interchange §3, O1, O2)."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from canonic.config import CanonicConfig, Connection, ProjectConfig
from canonic.connectors.base import (
    AcquisitionTier,
    CandidateKind,
    Capability,
    DefinitionEntityType,
    DefinitionEvidence,
    DocEvidence,
    ReviewFlag,
    UsageHint,
)
from canonic.connectors.factory import default_factory
from canonic.connectors.ossie import OssieConnector
from canonic.connectors.ossie_sql import render, select_expression
from canonic.exc import ConnectionError, UnsupportedSourceVersionError
from canonic.semantic.models import Additivity, Relationship

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
        assert any("dataset recent_orders is query-sourced" in w for w in health.warnings)

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
        assert "Ossie shop: dataset recent_orders is query-sourced" in caplog.text

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
        assert docs["ossie:shop#metric/revenue"].topic_refs == ["warehouse.orders.revenue"]
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


async def _definitions(
    path: Path, target_dialect: str | None = None
) -> dict[tuple[DefinitionEntityType, str], DefinitionEvidence]:
    connector = OssieConnector([str(path)], source="warehouse", target_dialect=target_dialect)
    extract = await connector.extract_definitions()
    return {(d.entity_type, d.entity): d for d in extract.definitions}


class TestDialectSelection:
    def test_native_variant_wins_for_its_target(self) -> None:
        variants = [("ANSI_SQL", "LOWER(email)"), ("SNOWFLAKE", "LOWER(email)::VARCHAR")]
        node = select_expression(variants, "snowflake")
        assert render(node, "snowflake") == "CAST(LOWER(email) AS VARCHAR)"

    def test_portable_variant_wins_without_native_match(self) -> None:
        variants = [("SNOWFLAKE", "LOWER(email)::VARCHAR"), ("ANSI_SQL", "LOWER(email)")]
        assert render(select_expression(variants, "postgres"), "postgres") == "LOWER(email)"

    def test_ossie_sql_is_preferred_over_ansi(self) -> None:
        variants = [("ANSI_SQL", "UPPER(a)"), ("OSSIE_SQL_2026", "LOWER(a)")]
        assert render(select_expression(variants, None), None) == "LOWER(a)"

    def test_other_sql_dialect_is_transpiled(self) -> None:
        node = select_expression([("BIGQUERY", "SAFE_CAST(x AS STRING)")], "duckdb")
        assert render(node, "duckdb") == "TRY_CAST(x AS TEXT)"

    @pytest.mark.parametrize("dialect", ["MDX", "TABLEAU", "MAQL", "DAX", "SIGMA", "THOUGHTSPOT"])
    def test_non_sql_dialect_is_unmappable(self, dialect: str) -> None:
        with pytest.raises(
            ValueError, match=f"no SQL dialect among the expression variants \\({dialect}\\)"
        ):
            select_expression([(dialect, "[Measures].[x]")], None)

    def test_parse_error_is_one_plain_line(self) -> None:
        with pytest.raises(ValueError) as info:
            select_expression([("ANSI_SQL", "SUM(a")], None)
        message = str(info.value)
        assert message.startswith("ANSI_SQL expression does not parse")
        assert "\n" not in message
        assert "\x1b" not in message

    async def test_snowflake_target_uses_snowflake_variant(self, ossie_model_path: Path) -> None:
        """O2 AC4."""
        snowflake = await _definitions(ossie_model_path, "snowflake")
        generic = await _definitions(ossie_model_path)
        key = (DefinitionEntityType.MEASURE, "order_count")
        assert snowflake[key].expr == "COUNT(TRY_TO_NUMBER(order_id))"
        assert generic[key].expr == "COUNT(order_id)"


class TestDimensions:
    async def test_bare_column_becomes_column(self, ossie_model_path: Path) -> None:
        dim = (await _definitions(ossie_model_path))[(DefinitionEntityType.DIMENSION, "status")]
        assert dim.column == "status"
        assert dim.expr is None
        assert dim.references == ["main.orders"]
        assert dim.description == "Order status"
        assert dim.native_ref == "ossie:shop/orders/status"

    async def test_computed_field_becomes_expr(self, ossie_model_path: Path) -> None:
        defs = await _definitions(ossie_model_path)
        dim = defs[(DefinitionEntityType.DIMENSION, "status_upper")]
        assert dim.column is None
        assert dim.expr == "UPPER(status)"

    async def test_synonyms_become_aliases(self, ossie_model_path: Path) -> None:
        dim = (await _definitions(ossie_model_path))[(DefinitionEntityType.DIMENSION, "status")]
        assert dim.aliases == ["order state", "state"]

    async def test_temporal_datatype_defaults_is_time(self, ossie_model_path: Path) -> None:
        defs = await _definitions(ossie_model_path)
        assert defs[(DefinitionEntityType.DIMENSION, "order_date")].is_time is True
        assert defs[(DefinitionEntityType.DIMENSION, "status")].is_time is False

    async def test_explicit_is_time_wins_over_datatype(self, tmp_path: Path) -> None:
        path = _write(
            tmp_path,
            "version: 0.2.0.dev0\nname: m\ndatasets:\n"
            "  - name: d\n    source: t\n    fields:\n"
            "      - name: created_at\n        datatype: DateTime\n"
            "        dimension: {is_time: false}\n"
            "        expression: {dialects: [{dialect: ANSI_SQL, expression: created_at}]}\n",
        )
        defs = await _definitions(path)
        assert defs[(DefinitionEntityType.DIMENSION, "created_at")].is_time is False

    async def test_field_without_dimension_is_not_a_dimension(self, ossie_model_path: Path) -> None:
        defs = await _definitions(ossie_model_path)
        assert (DefinitionEntityType.DIMENSION, "amount") not in defs


class TestMetrics:
    async def test_sum_is_additive_on_its_dataset(self, ossie_model_path: Path) -> None:
        """O3 AC1."""
        measure = (await _definitions(ossie_model_path))[(DefinitionEntityType.MEASURE, "revenue")]
        assert measure.expr == "SUM(amount)"
        assert measure.additivity == Additivity.ADDITIVE
        assert measure.references == ["main.orders"]
        assert measure.aliases == ["sales"]
        assert measure.review_flags == []

    async def test_count_distinct_proposes_distinct_count(self, ossie_model_path: Path) -> None:
        """O3 AC2."""
        defs = await _definitions(ossie_model_path)
        measure = defs[(DefinitionEntityType.MEASURE, "customer_count")]
        assert measure.additivity == Additivity.NON_ADDITIVE
        candidate = defs[(DefinitionEntityType.METRIC, "customer_count")].contract_candidate
        assert candidate is not None
        assert candidate.kind == CandidateKind.DISTINCT_COUNT
        assert candidate.measures == ["customer_count"]

    async def test_avg_is_flagged(self, ossie_model_path: Path) -> None:
        defs = await _definitions(ossie_model_path)
        measure = defs[(DefinitionEntityType.MEASURE, "average_order_value")]
        assert measure.additivity == Additivity.NON_ADDITIVE
        assert measure.review_flags == [ReviewFlag.AVG_SUGGESTS_RATIO]

    async def test_ratio_yields_components_and_a_candidate(self, ossie_model_path: Path) -> None:
        """O3 AC3: two component measures and a ratio proposal, no measure for the ratio."""
        defs = await _definitions(ossie_model_path)
        assert (DefinitionEntityType.MEASURE, "revenue_per_customer") not in defs
        denominator = defs[(DefinitionEntityType.MEASURE, "revenue_per_customer_denominator")]
        assert denominator.expr == "COUNT(DISTINCT customer_id)"
        assert denominator.references == ["main.customers"]
        metric = defs[(DefinitionEntityType.METRIC, "revenue_per_customer")]
        assert metric.contract_candidate is not None
        assert metric.contract_candidate.kind == CandidateKind.RATIO
        # The numerator reuses the existing ``revenue`` measure instead of duplicating it.
        assert metric.contract_candidate.measures == ["revenue", "revenue_per_customer_denominator"]
        assert metric.references == ["main.orders", "main.customers"]

    async def test_unclassifiable_is_unknown_and_flagged(self, ossie_model_path: Path) -> None:
        """O3 AC4."""
        defs = await _definitions(ossie_model_path)
        measure = defs[(DefinitionEntityType.MEASURE, "running_revenue")]
        assert measure.additivity is None
        assert measure.review_flags == [ReviewFlag.UNCLASSIFIED_AGGREGATION]
        assert measure.expr == "SUM(amount) OVER (ORDER BY order_date)"

    async def test_count_star_belongs_to_the_only_dataset(self, tmp_path: Path) -> None:
        path = _write(
            tmp_path,
            "version: 0.2.0.dev0\nname: m\ndatasets: [{name: d, source: s.t}]\n"
            "metrics:\n  - name: rows\n"
            "    expression: {dialects: [{dialect: ANSI_SQL, expression: 'COUNT(*)'}]}\n",
        )
        measure = (await _definitions(path))[(DefinitionEntityType.MEASURE, "rows")]
        assert measure.references == ["s.t"]
        assert measure.additivity == Additivity.ADDITIVE

    async def test_count_star_is_ambiguous_across_datasets(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        path = _write(
            tmp_path,
            "version: 0.2.0.dev0\nname: m\n"
            "datasets: [{name: a, source: a}, {name: b, source: b}]\n"
            "metrics:\n  - name: rows\n"
            "    expression: {dialects: [{dialect: ANSI_SQL, expression: 'COUNT(*)'}]}\n",
        )
        with caplog.at_level(logging.WARNING, logger="canonic.connectors.ossie"):
            defs = await _definitions(path)
        assert (DefinitionEntityType.MEASURE, "rows") not in defs
        assert "metric rows: references no column, so its dataset is ambiguous" in caplog.text

    async def test_bare_column_resolves_through_field_names(self, tmp_path: Path) -> None:
        path = _write(
            tmp_path,
            "version: 0.2.0.dev0\nname: m\ndatasets:\n"
            "  - {name: a, source: a, fields: [{name: x, expression: {dialects: []}}]}\n"
            "  - {name: b, source: b, fields: [{name: y, expression: {dialects: []}}]}\n"
            "metrics:\n  - name: total_y\n"
            "    expression: {dialects: [{dialect: ANSI_SQL, expression: 'SUM(y)'}]}\n",
        )
        measure = (await _definitions(path))[(DefinitionEntityType.MEASURE, "total_y")]
        assert measure.references == ["b"]


class TestRelationships:
    async def test_key_on_target_is_many_to_one_with_on(self, ossie_model_path: Path) -> None:
        defs = await _definitions(ossie_model_path)
        join = defs[(DefinitionEntityType.JOIN, "orders_to_customers")]
        assert join.review_flags == []
        assert join.references == ["main.orders", "main.customers"]
        (spec,) = join.joins
        assert (spec.left, spec.right) == ("orders", "customers")
        assert spec.relationship == Relationship.MANY_TO_ONE
        assert spec.on == "orders.customer_id = customers.customer_id"

    async def test_keys_on_both_sides_are_one_to_one(self, ossie_model_path: Path) -> None:
        defs = await _definitions(ossie_model_path)
        (spec,) = defs[(DefinitionEntityType.JOIN, "profiles_to_customers")].joins
        assert spec.relationship == Relationship.ONE_TO_ONE

    async def test_target_columns_off_key_are_flagged(self, ossie_model_path: Path) -> None:
        """O3 AC5, adjusted to Ossie's declared many-to-one direction."""
        join = (await _definitions(ossie_model_path))[
            (DefinitionEntityType.JOIN, "customers_by_country")
        ]
        assert join.joins[0].relationship == Relationship.MANY_TO_ONE
        assert join.review_flags == [ReviewFlag.JOIN_COLUMNS_NOT_A_KEY]

    async def test_composite_key_is_and_joined(self, tmp_path: Path) -> None:
        path = _write(
            tmp_path,
            "version: 0.2.0.dev0\nname: m\ndatasets:\n"
            "  - {name: lines, source: s.lines}\n"
            "  - {name: products, source: s.products, primary_key: [id, variant_id]}\n"
            "relationships:\n  - name: lines_to_products\n    from: lines\n    to: products\n"
            "    from_columns: [product_id, variant_id]\n    to_columns: [id, variant_id]\n",
        )
        (spec,) = (await _definitions(path))[(DefinitionEntityType.JOIN, "lines_to_products")].joins
        assert spec.on == (
            "lines.product_id = products.id AND lines.variant_id = products.variant_id"
        )
        assert spec.relationship == Relationship.MANY_TO_ONE


class TestUnmappable:
    async def test_every_unmappable_object_is_named(self, ossie_model_path: Path) -> None:
        """O2 AC3: a warning naming the object, nothing silently dropped."""
        health = await _connector(ossie_model_path).test_connection()
        named = [
            "field orders.pivot_status: no SQL dialect among the expression variants (TABLEAU)",
            "dataset recent_orders is query-sourced",
            "metric revenue_mdx: no SQL dialect among the expression variants (MDX)",
            "metric broken: ANSI_SQL expression does not parse",
            "relationship orders_to_recent: references an unknown or query-sourced dataset",
        ]
        for fragment in named:
            assert any(fragment in w for w in health.warnings), fragment
        assert len(health.warnings) == len(named)

    async def test_fingerprints_cover_semantic_fields(self, tmp_path: Path) -> None:
        base = (
            "version: 0.2.0.dev0\nname: m\ndatasets: [{name: d, source: s.t}]\n"
            "metrics:\n  - name: total\n"
            "    expression: {dialects: [{dialect: ANSI_SQL, expression: 'SUM(%s)'}]}\n"
        )
        first = await _definitions(_write(tmp_path, base % "a", "a.yaml"))
        second = await _definitions(_write(tmp_path, base % "b", "b.yaml"))
        key = (DefinitionEntityType.MEASURE, "total")
        assert first[key].source_fingerprint != second[key].source_fingerprint


class TestTargetDialectBinding:
    def test_config_binds_target_dialect(self) -> None:
        config = CanonicConfig(
            version=1,
            project=ProjectConfig(name="p"),
            connections=[
                Connection(id="warehouse", type="snowflake", params={"account": "a"}),
                Connection(
                    id="shop_ossie",
                    type="ossie",
                    params={"paths": ["m.yaml"], "target_connection": "warehouse"},
                ),
            ],
        )
        ossie = next(c for c in config.connections if c.id == "shop_ossie")
        assert ossie.target_dialect == "snowflake"
        assert "target_dialect" not in ossie.model_dump()

    def test_factory_passes_target_dialect(self, ossie_model_path: Path) -> None:
        conn = Connection(
            id="shop_ossie",
            type="ossie",
            params={"paths": [str(ossie_model_path)], "target_connection": "warehouse"},
        )
        conn.bind_target_dialect("snowflake")
        connector = default_factory.create(conn)
        assert isinstance(connector, OssieConnector)
        assert connector._target_dialect == "snowflake"  # noqa: SLF001
