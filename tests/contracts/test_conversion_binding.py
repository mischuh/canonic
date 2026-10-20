"""The ``conversion`` binding kind: shape, resolution and write-time validation."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

from canonic.contracts.models import (
    BindingKind,
    CanonicalRef,
    ConversionCalculation,
    ConversionPair,
    MetricBinding,
)
from canonic.contracts.resolver import Binding, ContractResolver
from canonic.contracts.validate import validate_contracts
from canonic.exc import ContractError

if TYPE_CHECKING:
    from pathlib import Path

VISITS_YAML = """\
name: visits
connection: wh
table: analytics.visits
grain: [visit_id]
columns:
  - { name: visit_id,   type: int,       nullable: false }
  - { name: user_id,    type: int,       nullable: false }
  - { name: visited_at, type: timestamp, nullable: false }
  - { name: visit_date, type: date,      nullable: false }
  - { name: channel,    type: string,    nullable: false }
  - { name: product,    type: string,    nullable: false }
measures:
  - { name: visits, expr: "count(visit_id)", additivity: additive }
dimensions:
  - { name: channel,     column: channel }
  - { name: visit_time,  column: visited_at }
  - { name: visit_month, column: visited_at, granularity: month }
  - { name: upper_channel, expr: "upper(channel)", type: string }
"""

PURCHASES_YAML = """\
name: purchases
connection: wh
table: analytics.purchases
grain: [purchase_id]
columns:
  - { name: purchase_id,  type: int,       nullable: false }
  - { name: buyer_id,     type: int,       nullable: false }
  - { name: purchased_at, type: timestamp, nullable: false }
  - { name: sku,          type: string,    nullable: false }
  - { name: amount,       type: decimal,   nullable: false }
measures:
  - { name: purchases, expr: "count(purchase_id)", additivity: additive }
  - { name: revenue,   expr: "sum(amount)",        additivity: additive }
  - { name: top_sale,  expr: "max(amount)",        additivity: additive }
  - { name: avg_sale,  expr: "avg(amount)",        additivity: non_additive }
dimensions:
  - { name: product, column: sku }
"""

BASE_METRICS = {
    "visits.yaml": "metric: visits\ncanonical: { source: visits, measure: visits }\n",
    "purchases.yaml": "metric: purchases\ncanonical: { source: purchases, measure: purchases }\n",
    "revenue.yaml": "metric: revenue\ncanonical: { source: purchases, measure: revenue }\n",
    "top_sale.yaml": "metric: top_sale\ncanonical: { source: purchases, measure: top_sale }\n",
    "avg_sale.yaml": "metric: avg_sale\ncanonical: { source: purchases, measure: avg_sale }\n",
    "buyers.yaml": (
        "metric: buyers\ncanonical: { kind: distinct_count, source: purchases, "
        "distinct_on: buyer_id }\n"
    ),
    "aov.yaml": "metric: aov\ncanonical: { kind: ratio, numerator: revenue, denominator: purchases }\n",
}


def _project(root: Path, metrics: dict[str, str]) -> Path:
    semantics = root / "semantics" / "wh"
    semantics.mkdir(parents=True)
    (semantics / "visits.yaml").write_text(VISITS_YAML)
    (semantics / "purchases.yaml").write_text(PURCHASES_YAML)
    folder = root / "contracts" / "metrics"
    folder.mkdir(parents=True)
    for name, content in {**BASE_METRICS, **metrics}.items():
        (folder / name).write_text(content)
    return root


def _conversion(name: str = "rate", **overrides: str) -> str:
    fields = {
        "base_metric": "visits",
        "conversion_metric": "purchases",
        "entity": "{base: user_id, conversion: buyer_id}",
        "base_time": "visit_time",
        "conversion_time": "purchased_at",
        "window": "7 days",
        **overrides,
    }
    body = "".join(f"  {k}: {v}\n" for k, v in fields.items() if v != "")
    return f"metric: {name}\ncanonical:\n  kind: conversion\n{body}"


def _ref(**overrides: Any) -> CanonicalRef:
    fields: dict[str, Any] = {
        "kind": "conversion",
        "base_metric": "visits",
        "conversion_metric": "purchases",
        "entity": "user_id",
        "base_time": "visited_at",
        "conversion_time": "purchased_at",
        **overrides,
    }
    return CanonicalRef(**{k: v for k, v in fields.items() if v is not None})


class TestShape:
    def test_requires_its_fields(self) -> None:
        for missing in ("base_metric", "conversion_metric", "entity", "base_time"):
            with pytest.raises(ValueError, match=f"requires '{missing}'"):
                _ref(**{missing: None})

    def test_an_entity_string_names_both_sides(self) -> None:
        assert _ref().entity == ConversionPair(base="user_id", conversion="user_id")
        pair = _ref(entity={"base": "user_id", "conversion": "buyer_id"}).entity
        assert pair == ConversionPair(base="user_id", conversion="buyer_id")

    def test_constant_properties_take_both_spellings(self) -> None:
        ref = _ref(constant_properties=["product", {"base": "channel", "conversion": "source"}])
        assert ref.constant_properties == [
            ConversionPair(base="product", conversion="product"),
            ConversionPair(base="channel", conversion="source"),
        ]

    def test_a_window_may_be_shorter_than_a_day(self) -> None:
        assert str(_ref(window="2 hours").window) == "2 hours"

    def test_a_population_filter_is_refused(self) -> None:
        with pytest.raises(ValueError, match="takes no population_filter"):
            _ref(population_filter="channel = 'ads'")

    @pytest.mark.parametrize(
        ("field", "value"), [("grain_to_date", "month"), ("period_agg", "first")]
    )
    def test_cumulative_frame_fields_are_refused(self, field: str, value: str) -> None:
        with pytest.raises(ValueError, match="only allowed on cumulative"):
            _ref(**{field: value})

    @pytest.mark.parametrize(
        ("field", "value"),
        [("entity", "user_id"), ("calculation", "conversions"), ("base_metric", "visits")],
    )
    def test_conversion_fields_are_refused_on_other_kinds(self, field: str, value: str) -> None:
        with pytest.raises(ValueError, match="only allowed on conversion"):
            CanonicalRef(source="visits", measure="visits", **{field: value})


class TestResolution:
    def test_resolves_both_metrics_and_names_the_window_in_its_key(self) -> None:
        resolver = ContractResolver(
            bindings=[
                MetricBinding(metric="visits", canonical=CanonicalRef(source="v", measure="n")),
                MetricBinding(metric="purchases", canonical=CanonicalRef(source="p", measure="n")),
                MetricBinding(metric="rate", canonical=_ref(window="7 days")),
            ],
            guardrails=(),
            finality=(),
            assertions=(),
        )
        rate = resolver.resolve_metric("rate")
        assert isinstance(rate, Binding)
        assert rate.kind is BindingKind.CONVERSION
        assert rate.conversion is not None
        assert rate.conversion.calculation is ConversionCalculation.CONVERSION_RATE
        assert rate.resolved_key == "conversion(v.n, p.n, conversion_rate, window=7 days)"
        assert [leaf.metric for leaf in rate.leaves()] == ["visits", "purchases"]
        assert [leaf.metric for leaf in rate.grouped_leaves()] == ["visits"]


class TestWriteTimeValidation:
    def test_valid_bindings_pass(self, tmp_path: Path) -> None:
        validate_contracts(
            _project(
                tmp_path,
                {
                    "rate.yaml": _conversion(),
                    "buyers_rate.yaml": _conversion(
                        "buyer_rate", conversion_metric="buyers", window=""
                    ),
                    "converted_revenue.yaml": _conversion(
                        "converted_revenue",
                        conversion_metric="revenue",
                        calculation="conversions",
                        window="2 hours",
                        constant_properties="[{base: product, conversion: sku}]",
                    ),
                    "share.yaml": (
                        'metric: share\ncanonical:\n  kind: derived\n  expr: "100 * rate"\n'
                        "  inputs: [{metric: rate}]\n"
                    ),
                },
            )
        )

    @pytest.mark.parametrize(
        ("metrics", "message"),
        [
            ({"x.yaml": _conversion(base_metric="nope")}, "base_metric 'nope' does not resolve"),
            ({"x.yaml": _conversion(conversion_metric="aov")}, "has kind ratio"),
            ({"x.yaml": _conversion(conversion_metric="avg_sale")}, "must sum or count"),
            ({"x.yaml": _conversion(conversion_metric="top_sale")}, "must sum or count"),
            (
                {"x.yaml": _conversion(entity="nope")},
                "entity base 'nope' is not a column, or a dimension backed by one column",
            ),
            ({"x.yaml": _conversion(entity="user_id")}, "entity conversion 'user_id'"),
            ({"x.yaml": _conversion(base_time="channel")}, "must be a date or timestamp"),
            ({"x.yaml": _conversion(base_time="upper_channel")}, "is not a column"),
            (
                {"x.yaml": _conversion(base_time="visit_date", window="3 hours")},
                "needs timestamps on both sides",
            ),
            (
                {"x.yaml": _conversion(constant_properties="[channel]")},
                "constant_properties conversion 'channel'",
            ),
            (
                {
                    "x.yaml": _conversion(),
                    "y.yaml": (
                        "metric: y\ncanonical: { kind: ratio, numerator: rate, "
                        "denominator: visits }\n"
                    ),
                },
                "write the ratio as a derived metric",
            ),
            (
                {
                    "x.yaml": _conversion(),
                    "y.yaml": (
                        "metric: y\ncanonical:\n  kind: offset\n  base: rate\n"
                        "  time_dimension: visit_time\n  offset: 1 year\n"
                    ),
                },
                "has kind conversion, which cannot be shifted yet",
            ),
        ],
    )
    def test_invalid_bindings(self, tmp_path: Path, metrics: dict[str, str], message: str) -> None:
        with pytest.raises(ContractError, match=message):
            validate_contracts(_project(tmp_path, metrics))
