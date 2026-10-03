"""Unit tests for the semantic-source Pydantic models (SPEC-E5 §2.1, §7)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError
from ruamel.yaml import YAML

from canonic.semantic.models import (
    Additivity,
    Dimension,
    Measure,
    NormalizedType,
    Relationship,
    SemanticSource,
    compute_dimension_fingerprint,
    compute_measure_fingerprint,
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


def test_dimension_with_column_and_expr_rejected() -> None:
    raw = _minimal(
        "  - { name: id, type: string }",
        dimensions='  - { name: d, column: id, expr: "upper(id)", type: string }',
    )
    with pytest.raises(ValidationError, match="exactly one of 'column' and 'expr'"):
        SemanticSource.model_validate(raw)


def test_dimension_with_neither_column_nor_expr_rejected() -> None:
    raw = _minimal("  - { name: id, type: string }", dimensions="  - { name: d }")
    with pytest.raises(ValidationError, match="exactly one of 'column' and 'expr'"):
        SemanticSource.model_validate(raw)


def test_expr_dimension_requires_type() -> None:
    raw = _minimal(
        "  - { name: id, type: string }",
        dimensions='  - { name: d, expr: "upper(id)" }',
    )
    with pytest.raises(ValidationError, match="requires 'type'"):
        SemanticSource.model_validate(raw)


def test_expr_dimension_references_undeclared_column() -> None:
    raw = _minimal(
        "  - { name: id, type: string }",
        dimensions='  - { name: d, expr: "upper(ghost)", type: string }',
    )
    with pytest.raises(ValidationError, match="undeclared column 'ghost'"):
        SemanticSource.model_validate(raw)


def test_unparseable_expr_dimension_rejected() -> None:
    raw = _minimal(
        "  - { name: id, type: string }",
        dimensions='  - { name: d, expr: "upper(", type: string }',
    )
    with pytest.raises(ValidationError, match="cannot parse expression"):
        SemanticSource.model_validate(raw)


def test_granularity_on_non_time_expr_rejected() -> None:
    raw = _minimal(
        "  - { name: id, type: string }",
        dimensions='  - { name: d, expr: "upper(id)", type: string, granularity: day }',
    )
    with pytest.raises(ValidationError, match="must be date or timestamp"):
        SemanticSource.model_validate(raw)


def test_expr_dimension_with_declared_columns_parses() -> None:
    raw = _minimal(
        "  - { name: created, type: int }",
        dimensions=(
            '  - { name: d, expr: "to_timestamp(created)", type: timestamp, granularity: day }'
        ),
    )
    src = SemanticSource.model_validate(raw)
    assert src.dimensions[0].backing_columns() == {"created"}


def test_json_path_dimension_parses() -> None:
    raw = _minimal(
        "  - { name: properties, type: json }",
        dimensions=(
            '  - { name: url, column: properties, json_path: ["$current_url"], type: string }'
        ),
    )
    dim = SemanticSource.model_validate(raw).dimensions[0]
    assert dim.json_path == ["$current_url"]
    assert dim.is_derived
    assert dim.backing_columns() == {"properties"}
    assert dim.value_type([]) is NormalizedType.STRING


def test_json_path_dimension_requires_type() -> None:
    raw = _minimal(
        "  - { name: properties, type: json }",
        dimensions='  - { name: url, column: properties, json_path: ["u"] }',
    )
    with pytest.raises(ValidationError, match="requires 'type'"):
        SemanticSource.model_validate(raw)


def test_json_path_dimension_requires_a_json_column() -> None:
    raw = _minimal(
        "  - { name: properties, type: string }",
        dimensions='  - { name: url, column: properties, json_path: ["u"], type: string }',
    )
    with pytest.raises(ValidationError, match="must be a json column"):
        SemanticSource.model_validate(raw)


def test_json_path_dimension_with_expr_rejected() -> None:
    raw = _minimal(
        "  - { name: properties, type: json }",
        dimensions=('  - { name: url, expr: "properties", json_path: ["u"], type: string }'),
    )
    with pytest.raises(ValidationError, match="cannot also set 'expr'"):
        SemanticSource.model_validate(raw)


def test_json_path_dimension_without_column_rejected() -> None:
    raw = _minimal(
        "  - { name: properties, type: json }",
        dimensions='  - { name: url, json_path: ["u"], type: string }',
    )
    with pytest.raises(ValidationError, match="exactly one of 'column' and 'expr'"):
        SemanticSource.model_validate(raw)


@pytest.mark.parametrize("path", ["[]", '[""]', '["it\'s"]', "['say \"hi\"']", "['a\\\\b']"])
def test_json_path_dimension_rejects_empty_and_unsafe_keys(path: str) -> None:
    raw = _minimal(
        "  - { name: properties, type: json }",
        dimensions=f"  - {{ name: url, column: properties, json_path: {path}, type: string }}",
    )
    with pytest.raises(ValidationError, match="json_path"):
        SemanticSource.model_validate(raw)


def test_granularity_on_non_time_json_path_rejected() -> None:
    raw = _minimal(
        "  - { name: properties, type: json }",
        dimensions=(
            '  - { name: d, column: properties, json_path: ["d"], type: string, granularity: day }'
        ),
    )
    with pytest.raises(ValidationError, match="must be date or timestamp"):
        SemanticSource.model_validate(raw)


class TestDimensionFingerprint:
    def _dim(self, **kwargs: object) -> Dimension:
        base: dict[str, object] = {
            "name": "d",
            "column": "properties",
            "json_path": ["a"],
            "type": "string",
        }
        return Dimension.model_validate({**base, **kwargs})

    def test_plain_column_dimension_has_none(self) -> None:
        assert compute_dimension_fingerprint(Dimension(name="d", column="id")) is None

    def test_json_path_dimension_has_a_stable_fingerprint(self) -> None:
        assert compute_dimension_fingerprint(self._dim()) == compute_dimension_fingerprint(
            self._dim()
        )

    @pytest.mark.parametrize(
        "change", [{"json_path": ["b"]}, {"json_path": ["a", "b"]}, {"column": "other"}]
    )
    def test_changing_the_path_or_column_changes_it(self, change: dict[str, object]) -> None:
        assert compute_dimension_fingerprint(self._dim()) != compute_dimension_fingerprint(
            self._dim(**change)
        )

    def test_expr_dimension_fingerprint_is_unchanged(self) -> None:
        """Pinned: adding ``json_path`` must not drift every existing derived dimension."""
        dim = Dimension(name="d", expr="upper(id)", type=NormalizedType.STRING)
        assert (
            compute_dimension_fingerprint(dim)
            == "sha256:b3baecb0dccbd6eac5a46b6f21f5feb3035141e40028145bb73f6dba467fb476"
        )


class TestIsP0Compilable:
    def test_additive_sum_is_compilable(self) -> None:
        assert Measure(name="r", expr="sum(amount)").is_p0_compilable is True

    def test_count_distinct_not_compilable(self) -> None:
        m = Measure(name="c", expr="count(distinct id)", additivity=Additivity.NON_ADDITIVE)
        assert m.is_p0_compilable is False

    def test_non_additive_flag_not_compilable(self) -> None:
        m = Measure(name="r", expr="sum(amount)", additivity=Additivity.NON_ADDITIVE)
        assert m.is_p0_compilable is False


class TestComputeMeasureFingerprint:
    def test_format_is_sha256_prefixed(self) -> None:
        fp = compute_measure_fingerprint(Measure(name="r", expr="sum(amount)"))
        assert fp.startswith("sha256:")

    def test_stable_for_same_expr(self) -> None:
        a = compute_measure_fingerprint(Measure(name="r", expr="sum(amount)"))
        b = compute_measure_fingerprint(Measure(name="other", expr="sum(amount)"))
        assert a == b  # fingerprint tracks the expr, not the measure name

    def test_changes_with_expr(self) -> None:
        a = compute_measure_fingerprint(Measure(name="r", expr="sum(amount)"))
        b = compute_measure_fingerprint(Measure(name="r", expr="sum(amount * fx_rate)"))
        assert a != b
