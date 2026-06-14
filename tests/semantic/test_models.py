"""Unit tests for the semantic-source Pydantic models (SPEC-E5 §2.1, §7)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError
from ruamel.yaml import YAML

from canon.semantic.models import (
    Additivity,
    Measure,
    NormalizedType,
    Relationship,
    SemanticSource,
)


def _load(yaml_str: str) -> dict:
    import io

    return YAML().load(io.StringIO(yaml_str))


def test_valid_source_parses(valid_source_yaml: str) -> None:
    src = SemanticSource.model_validate(_load(valid_source_yaml))
    assert src.name == "orders"
    assert src.grain == ["order_id"]
    assert src.columns[3].type is NormalizedType.DECIMAL
    assert src.measures[0].additivity is Additivity.ADDITIVE
    assert src.joins[0].relationship is Relationship.MANY_TO_ONE
    assert src.dimensions[1].granularity == "day"


def _minimal(columns: str, **extra: str) -> dict:
    # grain: [] keeps these focused on measure/dimension rules without the grain
    # check firing first.
    body = f"name: t\nconnection: c\ntable: s.t\ngrain: []\ncolumns:\n{columns}\n"
    for k, v in extra.items():
        body += f"{k}:\n{v}\n"
    return _load(body)


def test_grain_references_undeclared_column() -> None:
    raw = _load(
        "name: t\nconnection: c\ntable: s.t\ngrain: [missing]\n"
        "columns:\n  - { name: id, type: string }\n"
    )
    with pytest.raises(ValidationError, match="grain column 'missing'"):
        SemanticSource.model_validate(raw)


def test_measure_expr_references_undeclared_column() -> None:
    raw = _minimal(
        "  - { name: amount, type: decimal }",
        measures="  - { name: rev, expr: 'sum(nope)' }",
    )
    with pytest.raises(ValidationError, match="undeclared column 'nope'"):
        SemanticSource.model_validate(raw)


@pytest.mark.parametrize("expr", ["sum(amount)", "count(*)", "count(distinct id)", "min(amount)"])
def test_measure_expr_with_declared_columns_parses(expr: str) -> None:
    raw = _minimal(
        "  - { name: id, type: string }\n  - { name: amount, type: decimal }",
        measures=f"  - {{ name: m, expr: '{expr}' }}",
    )
    src = SemanticSource.model_validate(raw)
    assert src.measures[0].expr == expr


def test_unparseable_measure_expr_rejected() -> None:
    raw = _minimal(
        "  - { name: amount, type: decimal }",
        measures="  - { name: m, expr: 'sum(' }",
    )
    with pytest.raises(ValidationError, match="cannot parse expression"):
        SemanticSource.model_validate(raw)


def test_non_additive_measure_accepted_at_load() -> None:
    raw = _minimal(
        "  - { name: id, type: string }",
        measures="  - { name: c, expr: 'count(distinct id)', additivity: non_additive }",
    )
    src = SemanticSource.model_validate(raw)
    assert src.measures[0].additivity is Additivity.NON_ADDITIVE


def test_duplicate_column_names_rejected() -> None:
    raw = _load(
        "name: t\nconnection: c\ntable: s.t\ngrain: [id]\n"
        "columns:\n  - { name: id, type: string }\n  - { name: id, type: int }\n"
    )
    with pytest.raises(ValidationError, match="duplicate column name 'id'"):
        SemanticSource.model_validate(raw)


def test_dimension_references_undeclared_column() -> None:
    raw = _minimal(
        "  - { name: id, type: string }",
        dimensions="  - { name: d, column: ghost }",
    )
    with pytest.raises(ValidationError, match="undeclared column 'ghost'"):
        SemanticSource.model_validate(raw)


class TestIsP0Compilable:
    def test_additive_sum_is_compilable(self) -> None:
        assert Measure(name="r", expr="sum(amount)").is_p0_compilable is True

    def test_count_distinct_not_compilable(self) -> None:
        m = Measure(name="c", expr="count(distinct id)", additivity=Additivity.NON_ADDITIVE)
        assert m.is_p0_compilable is False

    def test_non_additive_flag_not_compilable(self) -> None:
        m = Measure(name="r", expr="sum(amount)", additivity=Additivity.NON_ADDITIVE)
        assert m.is_p0_compilable is False
