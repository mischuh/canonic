"""Tests for SemanticQuery validation and filter coercion."""

import pytest

from canonic.compiler.query import SemanticQuery


def test_filters_string_passthrough() -> None:
    sq = SemanticQuery(metrics=["revenue"], filters=["segment = 'smb'", "status = 'active'"])
    assert sq.filters == ["segment = 'smb'", "status = 'active'"]


def test_filters_dict_equals() -> None:
    sq = SemanticQuery(
        metrics=["churn_rate"],
        filters=[{"field": "segment", "operator": "EQUALS", "value": "smb"}],
    )
    assert sq.filters == ["segment = 'smb'"]


def test_filters_dict_not_equals() -> None:
    sq = SemanticQuery(
        metrics=["revenue"],
        filters=[{"field": "status", "operator": "NOT_EQUALS", "value": "churned"}],
    )
    assert sq.filters == ["status != 'churned'"]


def test_filters_dict_comparison_operators() -> None:
    sq = SemanticQuery(
        metrics=["revenue"],
        filters=[
            {"field": "arr", "operator": "GREATER_THAN", "value": 1000},
            {"field": "arr", "operator": "LESS_THAN_OR_EQUAL", "value": 5000},
        ],
    )
    assert sq.filters == ["arr > 1000", "arr <= 5000"]


def test_filters_dict_in_operator() -> None:
    sq = SemanticQuery(
        metrics=["revenue"],
        filters=[{"field": "plan", "operator": "IN", "value": ["starter", "pro"]}],
    )
    assert sq.filters == ["plan IN ('starter', 'pro')"]


def test_filters_dict_like_operator() -> None:
    sq = SemanticQuery(
        metrics=["revenue"],
        filters=[{"field": "name", "operator": "LIKE", "value": "Acme%"}],
    )
    assert sq.filters == ["name LIKE 'Acme%'"]


def test_filters_mixed_str_and_dict() -> None:
    sq = SemanticQuery(
        metrics=["revenue"],
        filters=[
            "status = 'active'",
            {"field": "segment", "operator": "EQUALS", "value": "smb"},
        ],
    )
    assert sq.filters == ["status = 'active'", "segment = 'smb'"]


def test_filters_dict_value_escapes_single_quotes() -> None:
    sq = SemanticQuery(
        metrics=["revenue"],
        filters=[{"field": "name", "operator": "EQUALS", "value": "O'Brien"}],
    )
    assert sq.filters == ["name = 'O''Brien'"]


def test_filters_dict_unknown_operator_raises() -> None:
    with pytest.raises(Exception, match="unknown filter operator"):
        SemanticQuery(
            metrics=["revenue"],
            filters=[{"field": "segment", "operator": "CONTAINS", "value": "smb"}],
        )


def test_filters_dict_missing_field_raises() -> None:
    with pytest.raises(Exception, match="missing 'field'"):
        SemanticQuery(
            metrics=["revenue"],
            filters=[{"operator": "EQUALS", "value": "smb"}],
        )


def test_filters_dict_in_requires_list_raises() -> None:
    with pytest.raises(Exception, match="IN requires a list"):
        SemanticQuery(
            metrics=["revenue"],
            filters=[{"field": "plan", "operator": "IN", "value": "starter"}],
        )
