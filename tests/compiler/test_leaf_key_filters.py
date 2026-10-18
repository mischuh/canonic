"""Leaf identity ignores parenthesisation and repeated predicates in the WHERE conditions."""

from __future__ import annotations

import pytest
import sqlglot
from sqlglot import exp

from canonic.compiler import SemanticQuery, compile
from canonic.compiler.leaf import _filter_key
from canonic.contracts.models import (
    AppliesTo,
    BindingKind,
    CanonicalRef,
    Guardrail,
    GuardrailKind,
    MetricBinding,
    Severity,
)
from canonic.contracts.resolver import ContractResolver
from canonic.semantic.models import Column, Dimension, Measure, SemanticSource


def _key(*conditions: str) -> tuple[str, ...]:
    parsed: list[exp.Expression] = [sqlglot.parse_one(c, dialect="postgres") for c in conditions]
    return _filter_key(parsed)


class TestFilterKey:
    def test_redundant_parentheses_do_not_change_the_key(self) -> None:
        assert _key("(x = 0)") == _key("x = 0")
        assert _key("((x = 0))") == _key("x = 0")

    def test_repeated_predicate_collapses(self) -> None:
        assert _key("(x = 0)", "x = 0") == _key("x = 0")

    def test_conjunction_is_split_into_its_predicates(self) -> None:
        assert _key("(x = 0 AND y = 1)") == _key("x = 0", "y = 1")
        assert _key("x = 0 AND y = 1", "x = 0") == _key("x = 0", "y = 1")

    def test_conjunct_order_is_kept(self) -> None:
        assert _key("x = 0", "y = 1") != _key("y = 1", "x = 0")

    def test_different_predicates_stay_different(self) -> None:
        assert _key("x = 0") != _key("x = 1")
        assert _key("x = 0") != _key("x = 0", "y = 1")

    def test_or_keeps_its_grouping(self) -> None:
        assert _key("x = 0 OR y = 1") == _key("(x = 0 OR y = 1)")
        assert _key("(x = 0 OR y = 1) AND z = 2") != _key("x = 0 OR (y = 1 AND z = 2)")

    def test_or_is_never_flattened_into_conjuncts(self) -> None:
        assert _key("(x = 0 OR y = 1)", "z = 2") == _key("(x = 0 OR y = 1) AND z = 2")
        assert _key("x = 0 OR y = 1") != _key("x = 0", "y = 1")


@pytest.fixture
def damages() -> SemanticSource:
    return SemanticSource(
        name="damages",
        connection="warehouse_pg",
        table="fct_damages",
        grain=["damage_id"],
        columns=[
            Column(name="damage_id", type="string", nullable=False),
            Column(name="repair_cost", type="decimal", nullable=True),
            Column(name="warranty_claim", type="int", nullable=False),
            Column(name="severity", type="string", nullable=False),
            Column(name="reported_month", type="string", nullable=False),
        ],
        measures=[
            Measure(name="total_repair_cost", expr="sum(repair_cost)", additivity="additive"),
            Measure(name="damage_count", expr="count(damage_id)", additivity="additive"),
        ],
        dimensions=[Dimension(name="reported_month", column="reported_month")],
    )


def _resolver(population_filter: str | None, guardrail_filter: str | None) -> ContractResolver:
    bindings = [
        MetricBinding(
            metric="a", canonical=CanonicalRef(source="damages", measure="total_repair_cost")
        ),
        MetricBinding(metric="b", canonical=CanonicalRef(source="damages", measure="damage_count")),
        MetricBinding(
            metric="r",
            canonical=CanonicalRef(
                kind=BindingKind.RATIO,
                numerator="a",
                denominator="b",
                population_filter=population_filter,
            ),
        ),
    ]
    guardrails = (
        [
            Guardrail(
                id="no-warranty",
                applies_to=AppliesTo(source="damages"),
                kind=GuardrailKind.MANDATORY_FILTER,
                filter=guardrail_filter,
                severity=Severity.ERROR,
                rationale="Excludes warranty claims.",
            )
        ]
        if guardrail_filter
        else []
    )
    return ContractResolver(bindings=bindings, guardrails=guardrails)


def _leaf_count(sql: str) -> int:
    return len(
        {
            m
            for m in sqlglot.parse_one(sql, dialect="postgres").find_all(exp.CTE)
            if m.alias.startswith("_leaf_")
        }
    )


def _compile_all(resolver: ContractResolver, damages: SemanticSource) -> str:
    query = SemanticQuery(metrics=["r", "a", "b"], dimensions=["reported_month"])
    return compile(query, resolver, [damages]).sql


def test_component_and_standalone_leaves_fuse_when_population_filter_equals_guardrail(
    damages: SemanticSource,
) -> None:
    sql = _compile_all(_resolver("warranty_claim = 0", "warranty_claim = 0"), damages)

    assert _leaf_count(sql) == 1


def test_leaves_with_a_different_population_filter_stay_apart(damages: SemanticSource) -> None:
    sql = _compile_all(_resolver("severity = 'major'", "warranty_claim = 0"), damages)

    assert _leaf_count(sql) == 2


def test_ratio_with_components_and_no_filters_fuses_into_one_leaf(damages: SemanticSource) -> None:
    sql = _compile_all(_resolver(None, None), damages)

    assert _leaf_count(sql) == 1
