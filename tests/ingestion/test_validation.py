"""Tests for canon/ingestion/validation.py (GH-36) — SPEC-E4 §10 validation gate.

The gate reuses the E2 schema probe and the E5 semantic/contract validators on the proposed
(not-yet-committed) state. Tests drive it with a fake connector (no live DB) and proposals
produced by the real ``ContextBuilder`` so the content shape matches production.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest

from canon.connectors.base import (
    AcquisitionTier,
    Capability,
    ColumnInfo,
    ConnectorBase,
    Health,
    RelationSchema,
    compute_fingerprint,
)
from canon.exc import ErrorCode, SchemaMismatch, ValidationFailed
from canon.ingestion.builder import ContextBuilder
from canon.ingestion.models import EvidenceItem, Proposal
from canon.ingestion.validation import ValidationGate, ViolationKind

if TYPE_CHECKING:
    from pathlib import Path

_NOW = datetime(2026, 6, 16, 12, 0, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


class FakeConnector(ConnectorBase):
    """A connector whose ``describe_relation`` returns canned observed columns."""

    def __init__(self, observed: dict[str, list[ColumnInfo]] | None = None) -> None:
        self._observed = observed or {}

    def capabilities(self) -> list[Capability]:
        return [Capability.CAPABILITIES, Capability.TEST_CONNECTION]

    async def test_connection(self) -> Health:
        return Health(status="ok")

    async def describe_relation(self, relation: str) -> list[ColumnInfo]:
        if relation not in self._observed:
            raise KeyError(f"relation {relation} does not exist")
        return self._observed[relation]


def _columns(*names_types: tuple[str, str]) -> list[ColumnInfo]:
    return [
        ColumnInfo(name=n, type=t, nullable=False, position=i + 1)
        for i, (n, t) in enumerate(names_types)
    ]


def _schema(
    *,
    columns: list[ColumnInfo],
    primary_key: list[str],
    tier: AcquisitionTier,
    relation: str = "analytics.orders",
) -> RelationSchema:
    return RelationSchema(
        connection="warehouse_pg",
        relation=relation,
        kind="table",
        columns=columns,
        primary_key=primary_key,
        acquisition_tier=tier,
        source_fingerprint=compute_fingerprint(columns, primary_key, []),
    )


def _evidence(schema: RelationSchema, tier: AcquisitionTier) -> EvidenceItem:
    return EvidenceItem(
        source=schema.connection,
        kind="relation_schema",
        acquisition_tier=tier,
        payload=schema.model_dump(mode="json"),
        source_fingerprint=schema.source_fingerprint or "sha256:none",
        observed_at=_NOW,
    )


async def _proposal_for(schema: RelationSchema) -> Proposal:
    """Run the real builder so the proposal's content is exactly production shape."""
    evidence = _evidence(schema, schema.acquisition_tier)
    result = await ContextBuilder().build([evidence])
    return result.proposals[0]


# ---------------------------------------------------------------------------
# S8-AC1 — semantic reference integrity blocks emission
# ---------------------------------------------------------------------------


class TestSemanticValidation:
    async def test_reference_integrity_failure_raises_validation_failed(
        self, tmp_path: Path
    ) -> None:
        """A proposal whose grain references an undeclared column → VALIDATION_FAILED (S8-AC1)."""
        # primary_key "ghost" is not among the declared columns → grain check fails in E5.
        schema = _schema(
            columns=_columns(("order_id", "int"), ("amount", "decimal")),
            primary_key=["ghost"],
            tier=AcquisitionTier.LIVE,
        )
        proposal = await _proposal_for(schema)
        gate = ValidationGate(
            tmp_path, connectors={}, evidence=[_evidence(schema, schema.acquisition_tier)]
        )

        with pytest.raises(ValidationFailed) as excinfo:
            await gate.validate([proposal])

        assert excinfo.value.code is ErrorCode.VALIDATION_FAILED
        (violation,) = excinfo.value.candidates
        assert violation.kind is ViolationKind.VALIDATION_FAILED
        assert violation.location.startswith("semantics/warehouse_pg/orders.yaml")
        assert "ghost" in violation.detail

    async def test_valid_proposal_passes(self, tmp_path: Path) -> None:
        """A self-consistent tier-1 proposal validates and the gate returns None."""
        schema = _schema(
            columns=_columns(("order_id", "int"), ("amount", "decimal")),
            primary_key=["order_id"],
            tier=AcquisitionTier.LIVE,
        )
        proposal = await _proposal_for(schema)
        gate = ValidationGate(
            tmp_path, connectors={}, evidence=[_evidence(schema, schema.acquisition_tier)]
        )

        assert await gate.validate([proposal]) is None


# ---------------------------------------------------------------------------
# S8-AC2 — tier 4–6 evidence must pass the live probe
# ---------------------------------------------------------------------------


class TestSchemaProbe:
    async def test_tier_four_six_probe_mismatch_raises_schema_mismatch(
        self, tmp_path: Path
    ) -> None:
        """Declarative evidence diverging from the live source → SCHEMA_MISMATCH (S8-AC2)."""
        # Declares a "ghost" column the live source does not observe → probe mismatch.
        # Content stays E5-valid (grain order_id is a declared column).
        schema = _schema(
            columns=_columns(("order_id", "int"), ("ghost", "int")),
            primary_key=["order_id"],
            tier=AcquisitionTier.DECLARATIVE,
        )
        proposal = await _proposal_for(schema)
        connector = FakeConnector(observed={"analytics.orders": _columns(("order_id", "int"))})
        gate = ValidationGate(
            tmp_path,
            connectors={"warehouse_pg": connector},
            evidence=[_evidence(schema, AcquisitionTier.DECLARATIVE)],
        )

        with pytest.raises(SchemaMismatch) as excinfo:
            await gate.validate([proposal])

        assert excinfo.value.code is ErrorCode.SCHEMA_MISMATCH
        (violation,) = excinfo.value.candidates
        assert violation.kind is ViolationKind.SCHEMA_MISMATCH
        assert violation.location == "analytics.orders"
        assert "ghost" in violation.detail

    async def test_missing_connector_is_a_hard_failure(self, tmp_path: Path) -> None:
        """Tier 4–6 evidence with no connector cannot be verified → SCHEMA_MISMATCH."""
        schema = _schema(
            columns=_columns(("order_id", "int")),
            primary_key=["order_id"],
            tier=AcquisitionTier.HAND_AUTHORED,
        )
        proposal = await _proposal_for(schema)
        gate = ValidationGate(
            tmp_path,
            connectors={},  # no connector for warehouse_pg
            evidence=[_evidence(schema, AcquisitionTier.HAND_AUTHORED)],
        )

        with pytest.raises(SchemaMismatch) as excinfo:
            await gate.validate([proposal])

        (violation,) = excinfo.value.candidates
        assert violation.kind is ViolationKind.SCHEMA_MISMATCH
        assert "no connector" in violation.detail

    async def test_tier_one_three_skip_the_probe(self, tmp_path: Path) -> None:
        """Live evidence is already source-derived and is never probed (no connector needed)."""
        schema = _schema(
            columns=_columns(("order_id", "int")),
            primary_key=["order_id"],
            tier=AcquisitionTier.LIVE,
        )
        proposal = await _proposal_for(schema)
        gate = ValidationGate(
            tmp_path, connectors={}, evidence=[_evidence(schema, AcquisitionTier.LIVE)]
        )

        assert await gate.validate([proposal]) is None


# ---------------------------------------------------------------------------
# Aggregation & the frozen serving contract
# ---------------------------------------------------------------------------


class TestAggregationAndContract:
    async def test_all_violations_are_aggregated(self, tmp_path: Path) -> None:
        """A probe failure and an E5 failure surface together in one raised report."""
        probe_schema_rel = _schema(
            columns=_columns(("order_id", "int"), ("ghost", "int")),
            primary_key=["order_id"],
            tier=AcquisitionTier.DECLARATIVE,
            relation="analytics.orders",
        )
        live_schema_rel = _schema(
            columns=_columns(("id", "int")),
            primary_key=["missing"],  # grain references an undeclared column
            tier=AcquisitionTier.LIVE,
            relation="analytics.customers",
        )
        proposals = [await _proposal_for(probe_schema_rel), await _proposal_for(live_schema_rel)]
        connector = FakeConnector(observed={"analytics.orders": _columns(("order_id", "int"))})
        gate = ValidationGate(
            tmp_path,
            connectors={"warehouse_pg": connector},
            evidence=[
                _evidence(probe_schema_rel, AcquisitionTier.DECLARATIVE),
                _evidence(live_schema_rel, AcquisitionTier.LIVE),
            ],
        )

        # Mixed violation kinds → the gate raises ValidationFailed (not probe-only).
        with pytest.raises(ValidationFailed) as excinfo:
            await gate.validate(proposals)

        kinds = {v.kind for v in excinfo.value.candidates}
        assert kinds == {ViolationKind.SCHEMA_MISMATCH, ViolationKind.VALIDATION_FAILED}
        assert len(excinfo.value.candidates) == 2

    def test_validation_gate_reuses_existing_codes(self) -> None:
        """The gate raises only VALIDATION_FAILED / SCHEMA_MISMATCH — it introduces no new code."""
        assert {
            ErrorCode.VALIDATION_FAILED,
            ErrorCode.SCHEMA_MISMATCH,
        } <= set(ErrorCode)
