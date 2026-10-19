"""``numerator_parts`` on ratio and weighted_avg bindings: shape, write-time validation, outputs."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from pydantic import ValidationError

from canonic.compiler import SemanticQuery
from canonic.config import CanonicConfig
from canonic.contracts.models import CanonicalRef, MetricBinding, Status
from canonic.contracts.resolver import ContractResolver
from canonic.contracts.validate import validate_contracts
from canonic.core.models import BindingOut, QueryMetadata
from canonic.core.service import CanonicService
from canonic.exc import ContractError

if TYPE_CHECKING:
    from pathlib import Path

    from canonic.semantic.models import SemanticSource

LEDGER_YAML = """\
name: ledger
connection: warehouse_pg
table: analytics.fct_ledger
grain: [entry_id]
columns:
  - { name: entry_id,  type: int,     nullable: false }
  - { name: region_id, type: int,     nullable: false }
  - { name: amount,    type: decimal, nullable: false }
  - { name: account,   type: string,  nullable: false }
measures:
  - { name: net_result, expr: "sum(amount)", additivity: additive }
  - { name: revenue,    expr: "sum(amount)", additivity: additive }
  - { name: costs,      expr: "sum(amount)", additivity: additive }
  - { name: customers,  expr: "count(distinct entry_id)", additivity: non_additive }
joins:
  - { to: regions, on: "ledger.region_id = regions.region_id", relationship: many_to_one }
"""

REGIONS_YAML = """\
name: regions
connection: warehouse_pg
table: analytics.dim_regions
grain: [region_id]
columns:
  - { name: region_id, type: int,     nullable: false }
  - { name: budget,    type: decimal, nullable: false }
measures:
  - { name: budget, expr: "sum(budget)", additivity: additive }
"""

ISLAND_YAML = """\
name: island
connection: warehouse_pg
table: analytics.island
grain: [id]
columns:
  - { name: id,     type: int,     nullable: false }
  - { name: amount, type: decimal, nullable: false }
measures:
  - { name: amount, expr: "sum(amount)", additivity: additive }
"""

_SINGLES = {
    "net-result.yaml": "metric: net_result\ncanonical: {source: ledger, measure: net_result}\n",
    "revenue.yaml": "metric: revenue\ncanonical: {source: ledger, measure: revenue}\n",
    "costs.yaml": "metric: costs\ncanonical: {source: ledger, measure: costs}\n",
    "budget.yaml": "metric: budget\ncanonical: {source: regions, measure: budget}\n",
    "island.yaml": "metric: island_amount\ncanonical: {source: island, measure: amount}\n",
    "customers.yaml": (
        "metric: customers\ncanonical: {kind: distinct_count, source: ledger, distinct_on: entry_id}\n"
    ),
    "margin.yaml": (
        "metric: margin\ncanonical: {kind: ratio, numerator: costs, denominator: revenue}\n"
    ),
}


def _project(root: Path, parts: str, *, kind: str = "ratio") -> Path:
    semantics = root / "semantics" / "warehouse_pg"
    semantics.mkdir(parents=True)
    (semantics / "ledger.yaml").write_text(LEDGER_YAML)
    (semantics / "regions.yaml").write_text(REGIONS_YAML)
    (semantics / "island.yaml").write_text(ISLAND_YAML)
    metrics = root / "contracts" / "metrics"
    metrics.mkdir(parents=True)
    for name, content in _SINGLES.items():
        (metrics / name).write_text(content)
    if kind == "ratio":
        components = "  numerator: net_result\n  denominator: revenue\n"
    else:
        components = "  weighted_sum: net_result\n  weight: revenue\n"
    (metrics / "result-rate.yaml").write_text(
        f"metric: result_rate\ncanonical:\n  kind: {kind}\n{components}  numerator_parts: {parts}\n"
    )
    return root


class TestShape:
    def test_defaults_to_none(self) -> None:
        ref = CanonicalRef(kind="ratio", numerator="a", denominator="b")
        assert ref.numerator_parts is None

    def test_accepted_on_ratio_and_weighted_avg(self) -> None:
        ratio = CanonicalRef(
            kind="ratio", numerator="n", denominator="d", numerator_parts=["a", "b"]
        )
        weighted = CanonicalRef(
            kind="weighted_avg", weighted_sum="n", weight="d", numerator_parts=["a", "b"]
        )
        assert ratio.numerator_parts == ["a", "b"]
        assert weighted.numerator_parts == ["a", "b"]

    def test_rejected_on_other_kinds(self) -> None:
        with pytest.raises(ValidationError, match="only allowed on ratio"):
            CanonicalRef(source="s", measure="m", numerator_parts=["a", "b"])

    def test_requires_two_parts(self) -> None:
        with pytest.raises(ValidationError, match="at least two parts"):
            CanonicalRef(kind="ratio", numerator="n", denominator="d", numerator_parts=["a"])

    def test_rejects_duplicate_parts(self) -> None:
        with pytest.raises(ValidationError, match="more than once"):
            CanonicalRef(
                kind="ratio", numerator="n", denominator="d", numerator_parts=["a", "b", "a"]
            )


class TestValidation:
    @pytest.mark.parametrize("kind", ["ratio", "weighted_avg"])
    def test_valid_parts_pass(self, tmp_path: Path, kind: str) -> None:
        validate_contracts(_project(tmp_path, "[revenue, costs]", kind=kind))

    def test_part_on_a_joined_source_passes(self, tmp_path: Path) -> None:
        validate_contracts(_project(tmp_path, "[revenue, budget]"))

    def test_single_part_fails_at_load(self, tmp_path: Path) -> None:
        with pytest.raises(ContractError, match="at least two parts"):
            validate_contracts(_project(tmp_path, "[revenue]"))

    def test_unknown_part_fails(self, tmp_path: Path) -> None:
        with pytest.raises(ContractError, match="'refunds' does not resolve"):
            validate_contracts(_project(tmp_path, "[revenue, refunds]"))

    @pytest.mark.parametrize("part", ["margin", "customers"])
    def test_part_that_is_not_single_fails(self, tmp_path: Path, part: str) -> None:
        with pytest.raises(ContractError, match="every part must be a metric of kind single"):
            validate_contracts(_project(tmp_path, f"[revenue, {part}]"))

    def test_unreachable_part_fails(self, tmp_path: Path) -> None:
        with pytest.raises(ContractError, match="not reachable from the source of numerator"):
            validate_contracts(_project(tmp_path, "[revenue, island_amount]"))

    def test_weighted_avg_names_weighted_sum(self, tmp_path: Path) -> None:
        with pytest.raises(ContractError, match="source of weighted_sum 'net_result'"):
            validate_contracts(_project(tmp_path, "[revenue, island_amount]", kind="weighted_avg"))


@pytest.fixture
def parts_service(orders_source: SemanticSource, monkeypatch: pytest.MonkeyPatch) -> CanonicService:
    monkeypatch.setenv("PG_PASSWORD", "testpassword")
    singles = [
        MetricBinding(
            metric=name,
            canonical=CanonicalRef(source="orders", measure="total_revenue"),
            status=Status.ACTIVE,
        )
        for name in ("net_result", "revenue", "costs")
    ]
    ratio = MetricBinding(
        metric="result_rate",
        canonical=CanonicalRef(
            kind="ratio",
            numerator="net_result",
            denominator="revenue",
            numerator_parts=["revenue", "costs"],
        ),
        status=Status.ACTIVE,
    )
    plain = MetricBinding(
        metric="plain_rate",
        canonical=CanonicalRef(kind="ratio", numerator="costs", denominator="revenue"),
        status=Status.ACTIVE,
    )
    config = CanonicConfig.model_validate(
        {
            "version": 1,
            "project": {"name": "test", "default_connection": "warehouse_pg"},
            "connections": [
                {
                    "id": "warehouse_pg",
                    "type": "postgres",
                    "params": {"host": "localhost", "dbname": "testdb", "user": "test"},
                    "credentials_ref": "env:PG_PASSWORD",
                }
            ],
        }
    )
    resolver = ContractResolver(bindings=[*singles, ratio, plain], guardrails=[])
    return CanonicService(config=config, resolver=resolver, sources=[orders_source])


class TestOutputs:
    def test_describe_metric_returns_parts(self, parts_service: CanonicService) -> None:
        detail = parts_service.describe_metric("result_rate")
        assert detail.components is not None
        assert detail.components.numerator_parts == ["revenue", "costs"]

    def test_resolve_metric_returns_parts(self, parts_service: CanonicService) -> None:
        out = BindingOut.from_binding(parts_service.resolve_metric("result_rate"))
        assert out.components is not None
        assert out.components.numerator_parts == ["revenue", "costs"]

    def test_composition_carries_parts(self, parts_service: CanonicService) -> None:
        compiled = parts_service.compile_query(SemanticQuery(metrics=["result_rate"]))
        metadata = QueryMetadata.from_compile_result(compiled)
        assert metadata.composition["result_rate"].numerator_parts == [
            "revenue",
            "costs",
        ]

    def test_parts_are_none_when_not_declared(self, parts_service: CanonicService) -> None:
        detail = parts_service.describe_metric("plain_rate")
        compiled = parts_service.compile_query(SemanticQuery(metrics=["plain_rate"]))
        metadata = QueryMetadata.from_compile_result(compiled)
        assert detail.components is not None
        assert detail.components.numerator_parts is None
        assert metadata.composition["plain_rate"].numerator_parts is None
