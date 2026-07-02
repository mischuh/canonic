"""Tests for the schema acquisition ladder and validation probe (GH-7).

Unit tests cover the tier-4/6 builders, the probe diff (with a fake connector),
and the ladder's gap reporting with no database. Integration tests
(``@pytest.mark.integration``) exercise ``describe_relation`` and the probe
against a real PostgreSQL via testcontainers; they skip when Docker is absent.
"""

from __future__ import annotations

import pytest

from canonic.connectors.acquisition import (
    AcquisitionLadder,
    probe_schema,
    relations_from_ddl,
    relations_from_schema_yaml,
    relations_from_semantic_sources,
)
from canonic.connectors.base import (
    AcquisitionTier,
    Capability,
    ColumnInfo,
    ConnectorBase,
    Health,
    RelationSchema,
)
from canonic.exc import SchemaMismatch
from canonic.semantic.models import Column, NormalizedType, SemanticSource


def _relation(relation: str, columns: dict[str, str], **kw: object) -> RelationSchema:
    return RelationSchema(
        relation=relation,
        connection=kw.get("connection", "warehouse_pg"),  # type: ignore[arg-type]
        kind="table",
        columns=[ColumnInfo(name=n, type=t) for n, t in columns.items()],
        primary_key=kw.get("primary_key", []),  # type: ignore[arg-type]
        acquisition_tier=kw.get("tier", AcquisitionTier.HAND_AUTHORED),  # type: ignore[arg-type]
    )


class FakeConnector(ConnectorBase):
    """A connector with canned introspection and per-relation observed columns."""

    def __init__(
        self,
        *,
        live: list[RelationSchema] | None = None,
        observed: dict[str, list[ColumnInfo]] | None = None,
        capabilities: list[Capability] | None = None,
    ) -> None:
        self._live = live or []
        self._observed = observed or {}
        self._capabilities = capabilities or [Capability.CAPABILITIES, Capability.TEST_CONNECTION]

    def capabilities(self) -> list[Capability]:
        return self._capabilities

    async def test_connection(self) -> Health:
        return Health(status="ok")

    async def introspect_schema(self) -> list[RelationSchema]:
        return self._live

    async def describe_relation(self, relation: str) -> list[ColumnInfo]:
        if relation not in self._observed:
            raise KeyError(f"relation {relation} does not exist")
        return self._observed[relation]


# ---------------------------------------------------------------------------
# Tier 4: declarative DDL import
# ---------------------------------------------------------------------------


class TestRelationsFromDdl:
    _DDL = (
        "CREATE TABLE analytics.fct_orders ("
        "  order_id bigint PRIMARY KEY,"
        "  customer_id integer NOT NULL,"
        "  amount numeric(12,2),"
        "  metadata jsonb"
        ")"
    )

    def test_single_table(self) -> None:
        relations = relations_from_ddl(self._DDL, "warehouse_pg")
        assert len(relations) == 1
        rel = relations[0]
        assert rel.relation == "analytics.fct_orders"
        assert rel.connection == "warehouse_pg"
        assert rel.acquisition_tier is AcquisitionTier.DECLARATIVE
        assert rel.source_fingerprint is not None

    def test_normalized_types_and_pk(self) -> None:
        rel = relations_from_ddl(self._DDL, "warehouse_pg")[0]
        types = {c.name: c.type for c in rel.columns}
        assert types == {
            "order_id": "int",
            "customer_id": "int",
            "amount": "decimal",
            "metadata": "json",
        }
        assert rel.primary_key == ["order_id"]

    def test_nullability(self) -> None:
        rel = relations_from_ddl(self._DDL, "warehouse_pg")[0]
        by_name = {c.name: c for c in rel.columns}
        assert by_name["order_id"].nullable is False  # primary key
        assert by_name["customer_id"].nullable is False  # NOT NULL
        assert by_name["amount"].nullable is True

    def test_multiple_tables(self) -> None:
        ddl = "CREATE TABLE a (id int PRIMARY KEY);CREATE TABLE b (id int PRIMARY KEY, a_id int)"
        relations = relations_from_ddl(ddl, "warehouse_pg")
        assert sorted(r.relation for r in relations) == ["a", "b"]


# ---------------------------------------------------------------------------
# Tier 4: declarative schema YAML import
# ---------------------------------------------------------------------------


def test_relations_from_schema_yaml(tmp_path) -> None:  # noqa: ANN001
    path = tmp_path / "schema.yaml"
    path.write_text(
        "connection: warehouse_pg\n"
        "relation: analytics.dim_customers\n"
        "kind: table\n"
        "acquisition_tier: live\n"  # forced to declarative on import
        "columns:\n"
        "  - {name: customer_id, type: int, nullable: false}\n"
        "  - {name: name, type: string}\n"
    )
    relations = relations_from_schema_yaml(path)
    assert len(relations) == 1
    rel = relations[0]
    assert rel.relation == "analytics.dim_customers"
    assert rel.acquisition_tier is AcquisitionTier.DECLARATIVE  # forced
    assert {c.name for c in rel.columns} == {"customer_id", "name"}
    assert RelationSchema.model_validate(rel.model_dump()) == rel  # round-trips


# ---------------------------------------------------------------------------
# Tier 6: hand-authored semantic sources
# ---------------------------------------------------------------------------


def test_relations_from_semantic_sources() -> None:
    source = SemanticSource(
        name="orders",
        connection="warehouse_pg",
        table="analytics.fct_orders",
        grain=["order_id"],
        columns=[
            Column(name="order_id", type=NormalizedType.INT, nullable=False),
            Column(name="amount", type=NormalizedType.DECIMAL),
        ],
    )
    relations = relations_from_semantic_sources([source])
    assert len(relations) == 1
    rel = relations[0]
    assert rel.relation == "analytics.fct_orders"
    assert rel.acquisition_tier is AcquisitionTier.HAND_AUTHORED
    assert rel.primary_key == ["order_id"]
    assert {c.name: c.type for c in rel.columns} == {"order_id": "int", "amount": "decimal"}


# ---------------------------------------------------------------------------
# Validation probe (fake connector)
# ---------------------------------------------------------------------------


class TestProbeSchema:
    async def test_match_stamps_validated(self) -> None:
        declared = _relation("analytics.fct_orders", {"order_id": "int", "amount": "decimal"})
        connector = FakeConnector(
            observed={
                "analytics.fct_orders": [
                    ColumnInfo(name="order_id", type="int"),
                    ColumnInfo(name="amount", type="decimal"),
                ]
            }
        )
        result = await probe_schema(connector, declared)
        assert result.ok is True
        assert result.validated is not None
        assert result.validated.last_validated_at is not None
        assert result.validated.source_fingerprint is not None
        result.raise_for_status()  # must not raise

    async def test_missing_column(self) -> None:
        declared = _relation("analytics.fct_orders", {"order_id": "int", "amount": "decimal"})
        connector = FakeConnector(
            observed={"analytics.fct_orders": [ColumnInfo(name="order_id", type="int")]}
        )
        result = await probe_schema(connector, declared)
        assert result.ok is False
        assert result.missing_columns == ["amount"]
        assert result.validated is None
        with pytest.raises(SchemaMismatch, match="amount"):
            result.raise_for_status()

    async def test_type_conflict(self) -> None:
        declared = _relation("analytics.fct_orders", {"order_id": "int"})
        connector = FakeConnector(
            observed={"analytics.fct_orders": [ColumnInfo(name="order_id", type="string")]}
        )
        result = await probe_schema(connector, declared)
        assert result.ok is False
        assert [c.column for c in result.type_conflicts] == ["order_id"]
        with pytest.raises(SchemaMismatch, match="order_id"):
            result.raise_for_status()

    async def test_extra_column_is_non_fatal(self) -> None:
        declared = _relation("analytics.fct_orders", {"order_id": "int"})
        connector = FakeConnector(
            observed={
                "analytics.fct_orders": [
                    ColumnInfo(name="order_id", type="int"),
                    ColumnInfo(name="extra", type="string"),
                ]
            }
        )
        result = await probe_schema(connector, declared)
        assert result.ok is True
        assert result.extra_columns == ["extra"]

    async def test_missing_relation(self) -> None:
        declared = _relation("analytics.absent", {"order_id": "int"})
        result = await probe_schema(FakeConnector(), declared)
        assert result.ok is False
        assert result.missing_columns == ["order_id"]


# ---------------------------------------------------------------------------
# AcquisitionLadder orchestration
# ---------------------------------------------------------------------------


class TestAcquisitionLadder:
    async def test_partial_introspection_reports_gap(self) -> None:
        live = [_relation("analytics.a", {"id": "int"}, tier=AcquisitionTier.LIVE)]
        connector = FakeConnector(
            live=live,
            capabilities=[Capability.INTROSPECT_SCHEMA, Capability.CAPABILITIES],
        )
        ladder = AcquisitionLadder(connector)
        result = await ladder.acquire(expected_relations=["analytics.a", "analytics.b"])
        assert [s.relation for s in result.schemas] == ["analytics.a"]
        assert result.gap.missing_relations == ["analytics.b"]
        assert result.gap.has_gap is True

    async def test_blocked_catalog_supplemented_and_probed(self) -> None:
        source = SemanticSource(
            name="orders",
            connection="warehouse_pg",
            table="analytics.fct_orders",
            grain=["order_id"],
            columns=[Column(name="order_id", type=NormalizedType.INT, nullable=False)],
        )
        connector = FakeConnector(  # no INTROSPECT_SCHEMA capability ⇒ blocked
            observed={"analytics.fct_orders": [ColumnInfo(name="order_id", type="int")]},
        )
        ladder = AcquisitionLadder(connector)
        result = await ladder.acquire(
            semantic_sources=[source], expected_relations=["analytics.fct_orders"]
        )
        assert len(result.schemas) == 1
        acquired = result.schemas[0]
        assert acquired.acquisition_tier is AcquisitionTier.HAND_AUTHORED
        assert acquired.last_validated_at is not None  # probe stamped it
        assert result.gap.has_gap is False

    async def test_probe_mismatch_aborts_acquire(self) -> None:
        source = SemanticSource(
            name="orders",
            connection="warehouse_pg",
            table="analytics.fct_orders",
            grain=["missing_col"],
            columns=[Column(name="missing_col", type=NormalizedType.INT, nullable=False)],
        )
        connector = FakeConnector(
            observed={"analytics.fct_orders": [ColumnInfo(name="order_id", type="int")]},
        )
        ladder = AcquisitionLadder(connector)
        with pytest.raises(SchemaMismatch):
            await ladder.acquire(semantic_sources=[source])

    async def test_live_wins_over_declared_duplicate(self) -> None:
        live = [_relation("analytics.fct_orders", {"id": "int"}, tier=AcquisitionTier.LIVE)]
        connector = FakeConnector(
            live=live,
            capabilities=[Capability.INTROSPECT_SCHEMA, Capability.CAPABILITIES],
        )
        ladder = AcquisitionLadder(connector)
        result = await ladder.acquire(
            ddl="CREATE TABLE analytics.fct_orders (other int)", connection="warehouse_pg"
        )
        assert len(result.schemas) == 1
        assert result.schemas[0].acquisition_tier is AcquisitionTier.LIVE


# ---------------------------------------------------------------------------
# Integration: probe against a real PostgreSQL
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_describe_relation_observes_columns(pg_connector) -> None:  # noqa: ANN001
    columns = await pg_connector.describe_relation("analytics.fct_orders")
    by_name = {c.name: c.type for c in columns}
    assert by_name == {
        "order_id": "int",
        "customer_id": "int",
        "amount": "decimal",
        "metadata": "json",
        "order_date": "date",
    }


@pytest.mark.integration
async def test_probe_matches_live_schema(pg_connector) -> None:  # noqa: ANN001
    declared = _relation(
        "analytics.fct_orders",
        {
            "order_id": "int",
            "customer_id": "int",
            "amount": "decimal",
            "metadata": "json",
            "order_date": "date",
        },
    )
    result = await probe_schema(pg_connector, declared)
    assert result.ok is True
    assert result.validated is not None
    assert result.validated.last_validated_at is not None


@pytest.mark.integration
async def test_probe_detects_live_mismatch(pg_connector) -> None:  # noqa: ANN001
    declared = _relation("analytics.fct_orders", {"order_id": "string", "ghost": "int"})
    result = await probe_schema(pg_connector, declared)
    assert result.ok is False
    assert "ghost" in result.missing_columns
    assert any(c.column == "order_id" for c in result.type_conflicts)
    with pytest.raises(SchemaMismatch):
        result.raise_for_status()
