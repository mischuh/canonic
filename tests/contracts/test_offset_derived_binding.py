"""The ``offset`` and ``derived`` binding kinds: shape, resolution and write-time validation."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from canonic.contracts.models import BindingKind, CanonicalRef, MetricBinding
from canonic.contracts.resolver import Binding, ContractResolver
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
  - { name: order_date,  type: date,    nullable: false }
  - { name: category,    type: string,  nullable: false }
  - { name: amount,      type: decimal, nullable: false }
measures:
  - { name: revenue, expr: "sum(amount)", additivity: additive }
  - { name: orders,  expr: "count(order_id)", additivity: additive }
dimensions:
  - { name: order_date,  column: order_date }
  - { name: order_month, column: order_date, granularity: month }
  - { name: category,    column: category }
"""

SHIPMENTS_YAML = """\
name: shipments
connection: warehouse_pg
table: analytics.fct_shipments
grain: [shipment_id]
columns:
  - { name: shipment_id, type: int,  nullable: false }
  - { name: shipped_at,  type: date, nullable: false }
  - { name: ordered_at,  type: date, nullable: false }
measures:
  - { name: shipments, expr: "count(shipment_id)", additivity: additive }
dimensions:
  - { name: shipped_date, column: shipped_at }
  - { name: order_date,   column: ordered_at }
"""

BASE_METRICS = {
    "revenue.yaml": "metric: revenue\ncanonical: { source: orders, measure: revenue }\n",
    "orders.yaml": "metric: order_count\ncanonical: { source: orders, measure: orders }\n",
    "shipments.yaml": "metric: shipments\ncanonical: { source: shipments, measure: shipments }\n",
    "aov.yaml": (
        "metric: aov\ncanonical: { kind: ratio, numerator: revenue, denominator: order_count }\n"
    ),
    "running.yaml": (
        "metric: revenue_to_date\ncanonical:\n  kind: cumulative\n  source: orders\n"
        "  measure: revenue\n  order_by: [order_date]\n"
    ),
    "trailing.yaml": (
        "metric: revenue_7d\ncanonical:\n  kind: cumulative\n  source: orders\n"
        "  measure: revenue\n  order_by: [order_date]\n  window: 7 days\n"
    ),
}


def _project(root: Path, metrics: dict[str, str]) -> Path:
    semantics = root / "semantics" / "warehouse_pg"
    semantics.mkdir(parents=True)
    (semantics / "orders.yaml").write_text(ORDERS_YAML)
    (semantics / "shipments.yaml").write_text(SHIPMENTS_YAML)
    folder = root / "contracts" / "metrics"
    folder.mkdir(parents=True)
    for name, content in {**BASE_METRICS, **metrics}.items():
        (folder / name).write_text(content)
    return root


def _offset(name: str, base: str, *, dimension: str = "order_date", offset: str = "1 year") -> str:
    return (
        f"metric: {name}\ncanonical:\n  kind: offset\n  base: {base}\n"
        f"  time_dimension: {dimension}\n  offset: {offset}\n"
    )


def _derived(name: str, expr: str, inputs: str) -> str:
    return f'metric: {name}\ncanonical:\n  kind: derived\n  expr: "{expr}"\n  inputs: {inputs}\n'


class TestShape:
    def test_offset_requires_its_fields(self) -> None:
        for missing in ("base", "time_dimension", "offset"):
            fields = {"base": "revenue", "time_dimension": "order_date", "offset": "1 year"}
            del fields[missing]
            with pytest.raises(ValueError, match=f"requires '{missing}'"):
                CanonicalRef(kind="offset", **fields)

    @pytest.mark.parametrize(
        ("expr", "inputs", "message"),
        [
            ("a + b", [{"metric": "a"}], "not inputs"),
            ("a", [{"metric": "a"}, {"metric": "b"}], "not used in expr"),
            ("a + a2", [{"metric": "a"}, {"metric": "x", "alias": "a"}], "more than once"),
            ("CASE WHEN a > 0 THEN a END", [{"metric": "a"}], "may only use"),
            ("UPPER(a)", [{"metric": "a"}], "may only use"),
            ("a || 'x'", [{"metric": "a"}], "may only use"),
            ("o.a", [{"metric": "a"}], "may name inputs only"),
            ("a +", [{"metric": "a"}], "not valid SQL"),
        ],
    )
    def test_invalid_derived_expressions(
        self, expr: str, inputs: list[dict[str, str]], message: str
    ) -> None:
        with pytest.raises(ValueError, match=message):
            CanonicalRef(kind="derived", expr=expr, inputs=inputs)

    def test_allowed_functions(self) -> None:
        CanonicalRef(
            kind="derived",
            expr="ROUND(ABS(COALESCE(a, 0) - NULLIF(b, 0)) / GREATEST(a, LEAST(b, 1)), 2)",
            inputs=[{"metric": "a"}, {"metric": "b"}],
        )


class TestResolution:
    def test_offset_and_derived_resolve_to_their_inputs(self) -> None:
        bindings = [
            MetricBinding(metric="revenue", canonical=CanonicalRef(source="o", measure="r")),
            MetricBinding(
                metric="revenue_py",
                canonical=CanonicalRef(
                    kind="offset", base="revenue", time_dimension="order_date", offset="1y"
                ),
            ),
            MetricBinding(
                metric="yoy",
                canonical=CanonicalRef(
                    kind="derived",
                    expr="revenue / py - 1",
                    inputs=[{"metric": "revenue"}, {"metric": "revenue_py", "alias": "py"}],
                ),
            ),
        ]
        resolver = ContractResolver(bindings=bindings, guardrails=(), finality=(), assertions=())
        yoy = resolver.resolve_metric("yoy")
        assert isinstance(yoy, Binding)
        assert yoy.kind is BindingKind.DERIVED
        assert yoy.resolved_key == "derived(revenue / py - 1)"
        (revenue, offset) = yoy.dependencies()
        assert offset.resolved_key == "offset(o.r, order_date, 1 year)"
        assert [leaf.metric for leaf in yoy.leaves()] == ["revenue", "revenue"]


class TestWriteTimeValidation:
    def test_valid_bindings_pass(self, tmp_path: Path) -> None:
        validate_contracts(
            _project(
                tmp_path,
                {
                    "py.yaml": _offset("revenue_py", "revenue"),
                    "aov_py.yaml": _offset("aov_py", "aov", offset="1 month"),
                    "running_py.yaml": _offset("running_py", "revenue_to_date"),
                    "yoy.yaml": _derived(
                        "yoy",
                        "(revenue - py) / py",
                        "[{metric: revenue}, {metric: revenue_py, alias: py}]",
                    ),
                    "py2.yaml": _offset("revenue_2y", "revenue_py"),
                },
            )
        )

    @pytest.mark.parametrize(
        ("metrics", "message"),
        [
            ({"x.yaml": _offset("x", "nope")}, "base 'nope' does not resolve"),
            ({"x.yaml": _offset("x", "revenue", dimension="category")}, "date or timestamp"),
            ({"x.yaml": _offset("x", "revenue", dimension="nope")}, "is not reachable"),
            ({"x.yaml": _offset("x", "revenue_7d")}, "cannot be shifted yet"),
            (
                {
                    "py.yaml": _offset("shipments_py", "shipments", dimension="shipped_date"),
                    "x.yaml": _derived(
                        "mix",
                        "a + b",
                        "[{metric: revenue, alias: a}, {metric: shipments_py, alias: b}]",
                    ),
                    "y.yaml": _offset("y", "mix"),
                },
                "another time column",
            ),
            (
                {"x.yaml": _derived("x", "a + 1", "[{metric: nope, alias: a}]")},
                "input 'nope' does not resolve",
            ),
            (
                {
                    "x.yaml": _derived("x", "a + 1", "[{metric: y, alias: a}]"),
                    "y.yaml": _offset("y", "x"),
                },
                "cyclic dependency",
            ),
            (
                {
                    "py.yaml": _offset("revenue_py", "revenue"),
                    "r.yaml": (
                        "metric: r\ncanonical: { kind: ratio, numerator: revenue_py, "
                        "denominator: revenue }\n"
                    ),
                },
                "write the ratio as a derived metric",
            ),
        ],
    )
    def test_invalid_bindings(self, tmp_path: Path, metrics: dict[str, str], message: str) -> None:
        with pytest.raises(ContractError, match=message):
            validate_contracts(_project(tmp_path, metrics))
