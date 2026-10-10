"""The ``cumulative`` binding kind: its contract shape and its write-time validation."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from canonic.contracts.models import BindingKind, CanonicalRef, MetricBinding, OnGap
from canonic.contracts.resolver import Binding as ResolvedBinding
from canonic.contracts.resolver import ContractResolver, CumulativeBinding
from canonic.contracts.validate import validate_contracts
from canonic.exc import ContractError

if TYPE_CHECKING:
    from pathlib import Path

ORDERS_YAML = """\
name: orders
connection: warehouse_pg
table: analytics.fct_orders
grain: [order_id]
columns:
  - { name: order_id,    type: int,     nullable: false }
  - { name: order_date,  type: date,    nullable: true }
  - { name: category,    type: string,  nullable: false }
  - { name: amount,      type: decimal, nullable: false }
  - { name: customer_id, type: int,     nullable: false }
  - { name: is_gift,     type: bool,    nullable: false }
measures:
  - { name: revenue, expr: "sum(amount)", additivity: additive }
  - { name: buyers, expr: "count(distinct customer_id)", additivity: non_additive }
dimensions:
  - { name: order_day,   column: order_date, granularity: day }
  - { name: order_month, column: order_date, granularity: month }
  - { name: order_year,  expr: "EXTRACT(YEAR FROM order_date)",  type: int }
  - { name: order_mon,   expr: "EXTRACT(MONTH FROM order_date)", type: int }
  - { name: category,    column: category }
  - { name: is_gift,     column: is_gift }
joins:
  - { to: customers, on: "orders.customer_id = customers.customer_id", relationship: many_to_one }
"""

CUSTOMERS_YAML = """\
name: customers
connection: warehouse_pg
table: analytics.dim_customers
grain: [customer_id]
columns:
  - { name: customer_id, type: int,  nullable: false }
  - { name: signup_date, type: date, nullable: false }
dimensions:
  - { name: signup_day, column: signup_date, granularity: day }
"""


def _project(root: Path, canonical: str, *, extra: dict[str, str] | None = None) -> Path:
    semantics = root / "semantics" / "warehouse_pg"
    semantics.mkdir(parents=True)
    (semantics / "orders.yaml").write_text(ORDERS_YAML)
    (semantics / "customers.yaml").write_text(CUSTOMERS_YAML)
    metrics = root / "contracts" / "metrics"
    metrics.mkdir(parents=True)
    (metrics / "cumulative-revenue.yaml").write_text(
        f"metric: cumulative_revenue\ncanonical:\n{canonical}"
    )
    for name, content in (extra or {}).items():
        (metrics / name).write_text(content)
    return root


def _cumulative(order_by: str, *, measure: str = "revenue", source: str = "orders") -> str:
    return f"  kind: cumulative\n  source: {source}\n  measure: {measure}\n  order_by: {order_by}\n"


class TestShape:
    def test_on_gap_defaults_to_skip(self) -> None:
        ref = CanonicalRef(kind="cumulative", source="orders", measure="revenue", order_by=["d"])
        assert ref.on_gap is OnGap.SKIP

    def test_null_on_gap_is_skip(self) -> None:
        ref = CanonicalRef(
            kind="cumulative", source="orders", measure="revenue", order_by=["d"], on_gap=None
        )
        assert ref.on_gap is OnGap.SKIP

    def test_fill_observed_is_accepted(self) -> None:
        ref = CanonicalRef(
            kind="cumulative",
            source="orders",
            measure="revenue",
            order_by=["d"],
            on_gap="fill_observed",
        )
        assert ref.on_gap is OnGap.FILL_OBSERVED

    def test_unknown_on_gap_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="on_gap"):
            CanonicalRef(
                kind="cumulative",
                source="orders",
                measure="revenue",
                order_by=["d"],
                on_gap="fill_calendar",
            )

    @pytest.mark.parametrize("order_by", [None, []])
    def test_order_by_is_required(self, order_by: list[str] | None) -> None:
        with pytest.raises(ValueError, match="order_by"):
            CanonicalRef(kind="cumulative", source="orders", measure="revenue", order_by=order_by)

    def test_duplicate_order_dimension_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="more than once"):
            CanonicalRef(kind="cumulative", source="orders", measure="revenue", order_by=["d", "d"])

    @pytest.mark.parametrize("missing", ["source", "measure"])
    def test_source_and_measure_are_required(self, missing: str) -> None:
        fields = {"source": "orders", "measure": "revenue"}
        del fields[missing]
        with pytest.raises(ValueError, match=missing):
            CanonicalRef(kind="cumulative", order_by=["d"], **fields)

    def test_resolver_carries_order_by_and_on_gap(self) -> None:
        binding = MetricBinding(
            metric="cumulative_revenue",
            canonical=CanonicalRef(
                kind="cumulative",
                source="orders",
                measure="revenue",
                order_by=["order_day"],
                on_gap="fill_observed",
            ),
        )
        resolver = ContractResolver(bindings=[binding], guardrails=(), finality=(), assertions=())
        resolved = resolver.resolve_metric("cumulative_revenue")
        assert isinstance(resolved, ResolvedBinding)
        assert resolved.kind is BindingKind.CUMULATIVE
        assert (resolved.source, resolved.measure) == ("orders", "revenue")
        assert resolved.cumulative == CumulativeBinding(
            order_by=("order_day",), on_gap=OnGap.FILL_OBSERVED
        )
        assert resolved.resolved_key == "cumulative(orders.revenue)"


class TestWriteTimeValidation:
    @pytest.mark.parametrize(
        "order_by",
        [
            "[order_day]",
            "[order_month]",
            "[order_year, order_mon]",
            "[signup_day]",
            "[customers.signup_day]",
        ],
    )
    def test_valid_bindings_pass(self, tmp_path: Path, order_by: str) -> None:
        validate_contracts(_project(tmp_path, _cumulative(order_by)))

    def test_unknown_source(self, tmp_path: Path) -> None:
        with pytest.raises(ContractError, match="does not match any semantic source"):
            validate_contracts(_project(tmp_path, _cumulative("[order_day]", source="nope")))

    def test_unknown_measure(self, tmp_path: Path) -> None:
        with pytest.raises(ContractError, match="is not declared on source"):
            validate_contracts(_project(tmp_path, _cumulative("[order_day]", measure="nope")))

    def test_non_additive_measure(self, tmp_path: Path) -> None:
        with pytest.raises(ContractError, match="must be\\s+additive"):
            validate_contracts(_project(tmp_path, _cumulative("[order_day]", measure="buyers")))

    def test_unreachable_order_dimension(self, tmp_path: Path) -> None:
        with pytest.raises(ContractError, match="'nope' is not reachable"):
            validate_contracts(_project(tmp_path, _cumulative("[nope]")))

    @pytest.mark.parametrize(("dimension", "shown"), [("category", "string"), ("is_gift", "bool")])
    def test_unorderable_type(self, tmp_path: Path, dimension: str, shown: str) -> None:
        with pytest.raises(ContractError, match=f"has type '{shown}'"):
            validate_contracts(_project(tmp_path, _cumulative(f"[{dimension}]")))

    def test_same_column_at_two_granularities(self, tmp_path: Path) -> None:
        with pytest.raises(ContractError, match="both backed by column 'order_date'"):
            validate_contracts(_project(tmp_path, _cumulative("[order_month, order_day]")))

    def test_cumulative_is_not_a_ratio_component(self, tmp_path: Path) -> None:
        ratio = (
            "metric: share\n"
            "canonical:\n"
            "  kind: ratio\n"
            "  numerator: cumulative_revenue\n"
            "  denominator: revenue\n"
        )
        revenue = "metric: revenue\ncanonical:\n  source: orders\n  measure: revenue\n"
        root = _project(
            tmp_path,
            _cumulative("[order_day]"),
            extra={"share.yaml": ratio, "revenue.yaml": revenue},
        )
        with pytest.raises(ContractError, match="has kind cumulative"):
            validate_contracts(root)
