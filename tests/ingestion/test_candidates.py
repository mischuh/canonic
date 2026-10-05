"""Contract candidates: proposed metric bindings from a definition connector, never canonical."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from canonic.config import ReconcileConfig, scaffold_project
from canonic.connectors.base import (
    AcquisitionTier,
    CandidateKind,
    Capability,
    ColumnInfo,
    ConnectorBase,
    ContractCandidate,
    DefinitionEntityType,
    DefinitionEvidence,
    DefinitionExtract,
    Health,
    RelationSchema,
    compute_fingerprint,
)
from canonic.contracts.models import MetricBinding
from canonic.ingestion.builder import MODELING_REVIEW_CONFIDENCE, ContextBuilder
from canonic.ingestion.candidates import CANDIDATE_SENTINEL
from canonic.ingestion.models import EvidenceItem, EvidenceKind, ReconciliationDecision
from canonic.ingestion.pipeline import IngestionPipeline
from canonic.ingestion.reconciliation import (
    ExistingFact,
    InMemoryAcceptedStore,
    ReconciliationEngine,
)
from canonic.ingestion.source import evidence_from_definitions, evidence_from_introspection
from canonic.semantic.models import Additivity, Provenance

if TYPE_CHECKING:
    from pathlib import Path

_NOW = datetime(2026, 10, 5, 12, 0, 0, tzinfo=UTC)
_CONN = "warehouse"


def _schema(relation: str, columns: dict[str, str]) -> RelationSchema:
    cols = [ColumnInfo(name=n, type=t, nullable=True) for n, t in columns.items()]  # type: ignore[arg-type]
    pk = [next(iter(columns))]
    return RelationSchema(
        connection=_CONN,
        relation=relation,
        kind="table",
        columns=cols,
        primary_key=pk,
        foreign_keys=[],
        acquisition_tier=AcquisitionTier.LIVE,
        source_fingerprint=compute_fingerprint(cols, pk, []),
    )


_ORDERS = _schema("main.orders", {"order_id": "int", "customer_id": "int", "amount": "decimal"})
_CUSTOMERS = _schema("main.customers", {"customer_id": "int", "country": "string"})


def _definition(**fields: Any) -> DefinitionEvidence:
    fields.setdefault("native_ref", f"ossie:shop#metric/{fields['entity']}")
    return DefinitionEvidence(source=_CONN, acquisition_tier=AcquisitionTier.MODELING, **fields)


def _measure(
    name: str, expr: str, additivity: Additivity, relation: str = "main.orders", **kw: Any
) -> DefinitionEvidence:
    return _definition(
        entity=name,
        entity_type=DefinitionEntityType.MEASURE,
        expr=expr,
        additivity=additivity,
        references=[relation],
        **kw,
    )


def _candidate(
    name: str, kind: CandidateKind, measures: list[str], **kw: Any
) -> DefinitionEvidence:
    return _definition(
        entity=name,
        entity_type=DefinitionEntityType.METRIC,
        contract_candidate=ContractCandidate(kind=kind, measures=measures),
        **kw,
    )


def _shop_definitions() -> list[DefinitionEvidence]:
    return [
        _measure("revenue", "SUM(amount)", Additivity.ADDITIVE, aliases=["sales"]),
        _measure("customer_count", "COUNT(DISTINCT customer_id)", Additivity.NON_ADDITIVE),
        _candidate("customer_count", CandidateKind.DISTINCT_COUNT, ["customer_count"]),
        _measure(
            "revenue_per_customer_denominator",
            "COUNT(DISTINCT customer_id)",
            Additivity.NON_ADDITIVE,
            relation="main.customers",
        ),
        _candidate(
            "revenue_per_customer",
            CandidateKind.RATIO,
            ["revenue", "revenue_per_customer_denominator"],
            aliases=["arpc"],
        ),
    ]


def _item(evidence: RelationSchema | DefinitionEvidence) -> EvidenceItem:
    kind = (
        EvidenceKind.RELATION_SCHEMA
        if isinstance(evidence, RelationSchema)
        else EvidenceKind.DEFINITION
    )
    return EvidenceItem(
        source=_CONN,
        kind=kind,
        acquisition_tier=evidence.acquisition_tier,
        payload=evidence.model_dump(mode="json"),
        source_fingerprint=evidence.source_fingerprint or "sha256:none",
        observed_at=_NOW,
    )


async def _proposals(definitions: list[DefinitionEvidence]) -> Any:
    evidence = [_item(_ORDERS), _item(_CUSTOMERS), *(_item(d) for d in definitions)]
    return await ContextBuilder().build(evidence)


def _contracts(result: Any) -> dict[str, dict[str, Any]]:
    return {
        p.target: p.content for p in result.proposals if p.target.startswith("contracts/metrics/")
    }


class TestBuild:
    async def test_distinct_count_becomes_a_distinct_count_binding(self) -> None:
        contracts = _contracts(await _proposals(_shop_definitions()))
        binding = contracts["contracts/metrics/customer_count.yaml"]
        assert binding["canonical"] == {
            "kind": "distinct_count",
            "source": "orders",
            "distinct_on": "customer_id",
        }
        assert binding["provenance"] == "inferred"
        assert binding[CANDIDATE_SENTINEL] is True

    async def test_ratio_proposes_components_then_the_ratio(self) -> None:
        result = await _proposals(_shop_definitions())
        contracts = _contracts(result)
        targets = list(contracts)
        assert targets.index("contracts/metrics/revenue.yaml") < targets.index(
            "contracts/metrics/revenue_per_customer.yaml"
        )
        assert contracts["contracts/metrics/revenue.yaml"]["canonical"] == {
            "kind": "single",
            "source": "orders",
            "measure": "revenue",
        }
        assert contracts["contracts/metrics/revenue.yaml"]["aliases"] == ["sales"]
        denominator = contracts["contracts/metrics/revenue_per_customer_denominator.yaml"]
        assert denominator["canonical"]["kind"] == "distinct_count"
        assert denominator["canonical"]["source"] == "customers"
        ratio = contracts["contracts/metrics/revenue_per_customer.yaml"]
        assert ratio["canonical"] == {
            "kind": "ratio",
            "numerator": "revenue",
            "denominator": "revenue_per_customer_denominator",
        }
        assert ratio["aliases"] == ["arpc"]

    async def test_every_candidate_is_review_only(self) -> None:
        result = await _proposals(_shop_definitions())
        candidates = [p for p in result.proposals if p.target.startswith("contracts/")]
        assert candidates
        for proposal in candidates:
            assert proposal.provenance is Provenance.INFERRED
            assert proposal.confidence == MODELING_REVIEW_CONFIDENCE
            MetricBinding.model_validate(
                {k: v for k, v in proposal.content.items() if k != CANDIDATE_SENTINEL}
            )

    async def test_candidate_on_an_unplaced_measure_is_skipped(self) -> None:
        result = await _proposals([_candidate("ghosts", CandidateKind.DISTINCT_COUNT, ["ghosts"])])
        assert _contracts(result) == {}
        (skip,) = result.skipped
        assert "measure 'ghosts' was not placed on any relation" in skip.reason


def _accepted_binding(metric: str, canonical: dict[str, Any]) -> ExistingFact:
    binding = MetricBinding.model_validate({"metric": metric, "canonical": canonical})
    return ExistingFact(
        target=f"contracts/metrics/{metric}.yaml",
        content=binding.model_dump(mode="json"),
        provenance=binding.provenance,
    )


async def _reconcile(*facts: ExistingFact) -> dict[str, Any]:
    result = await _proposals(_shop_definitions())
    report = ReconciliationEngine().reconcile(result.proposals, InMemoryAcceptedStore(facts))
    return {e.target: e for e in report.entries if e.target.startswith("contracts/")}


class TestResolve:
    async def test_new_candidates_are_added_without_the_sentinel(self) -> None:
        entries = await _reconcile()
        assert {e.decision for e in entries.values()} == {ReconciliationDecision.ADD}
        for entry in entries.values():
            assert CANDIDATE_SENTINEL not in entry.proposal.content
            assert entry.auto_apply is False

    async def test_existing_binding_is_never_touched(self) -> None:
        """A curated binding with a different definition is a no-op, not a contradiction."""
        curated = _accepted_binding(
            "revenue", {"kind": "single", "source": "orders", "measure": "net_revenue"}
        )
        entries = await _reconcile(curated)
        revenue = entries["contracts/metrics/revenue.yaml"]
        assert revenue.decision is ReconciliationDecision.NO_OP
        assert "never change it" in (revenue.recommended_action or "")
        # The ratio still resolves: its numerator is bound by the existing binding.
        assert (
            entries["contracts/metrics/revenue_per_customer.yaml"].decision
            is ReconciliationDecision.ADD
        )

    async def test_candidate_whose_measure_will_not_exist_is_not_proposed(self) -> None:
        """A curated orders source the draft cannot edit leaves the measure missing."""
        curated_orders = ExistingFact(
            target=f"semantics/{_CONN}/orders.yaml",
            content={
                "name": "orders",
                "columns": [{"name": "order_id"}],
                "measures": [],
            },
            provenance=Provenance.HUMAN_CURATED,
            source_fingerprint="sha256:curated",
        )
        entries = await _reconcile(curated_orders)
        revenue = entries["contracts/metrics/revenue.yaml"]
        assert revenue.decision is ReconciliationDecision.NO_OP
        assert "measure 'revenue' will not exist on 'orders'" in (revenue.recommended_action or "")
        ratio = entries["contracts/metrics/revenue_per_customer.yaml"]
        assert ratio.decision is ReconciliationDecision.NO_OP
        assert "numerator 'revenue' has no binding" in (ratio.recommended_action or "")


class _Live(ConnectorBase):
    def capabilities(self) -> list[Capability]:
        return [Capability.INTROSPECT_SCHEMA, Capability.TEST_CONNECTION]

    async def test_connection(self) -> Health:
        return Health(status="ok")

    async def introspect_schema(self) -> list[RelationSchema]:
        return [_ORDERS, _CUSTOMERS]


class _Ossie(ConnectorBase):
    def capabilities(self) -> list[Capability]:
        return [Capability.EXTRACT_DEFINITIONS, Capability.TEST_CONNECTION]

    async def test_connection(self) -> Health:
        return Health(status="ok")

    async def extract_definitions(self) -> DefinitionExtract:
        return DefinitionExtract(definitions=_shop_definitions())


async def test_pipeline_validates_and_emits_candidates_for_review(tmp_path: Path) -> None:
    scaffold_project(tmp_path)
    connectors: dict[str, ConnectorBase] = {_CONN: _Live(), "shop_ossie": _Ossie()}
    pipeline = IngestionPipeline(tmp_path, connectors, ReconcileConfig())
    evidence = await evidence_from_introspection(connectors[_CONN], _CONN)
    evidence += await evidence_from_definitions(connectors["shop_ossie"], _CONN)

    result = await pipeline.run(evidence)

    contract_diffs = {d.target for d in result.emission.diffs if d.target.startswith("contracts/")}
    assert contract_diffs == {
        "contracts/metrics/customer_count.yaml",
        "contracts/metrics/revenue.yaml",
        "contracts/metrics/revenue_per_customer_denominator.yaml",
        "contracts/metrics/revenue_per_customer.yaml",
    }
    assert not any(d.auto_apply for d in result.emission.diffs if d.target in contract_diffs)
    assert not (tmp_path / "contracts" / "metrics" / "revenue.yaml").exists()


async def test_pipeline_run_with_an_existing_binding_does_not_fail(tmp_path: Path) -> None:
    """A candidate over an existing binding is a no-op, and the no-op refresh must skip it
    instead of loading the contract file as a semantic source."""
    scaffold_project(tmp_path)
    existing = tmp_path / "contracts" / "metrics" / "revenue.yaml"
    existing.parent.mkdir(parents=True, exist_ok=True)
    existing.write_text(
        "metric: revenue\ncanonical:\n  kind: single\n  source: orders\n  measure: revenue\n"
    )
    before = existing.read_text()
    connectors: dict[str, ConnectorBase] = {_CONN: _Live(), "shop_ossie": _Ossie()}
    pipeline = IngestionPipeline(tmp_path, connectors, ReconcileConfig())
    evidence = await evidence_from_introspection(connectors[_CONN], _CONN)
    evidence += await evidence_from_definitions(connectors["shop_ossie"], _CONN)

    result = await pipeline.run(evidence)

    revenue = next(e for e in result.report.entries if e.target == "contracts/metrics/revenue.yaml")
    assert revenue.decision is ReconciliationDecision.NO_OP
    assert existing.read_text() == before
