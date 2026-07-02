"""Tests for schema/table narrowing applied by connector introspection.

Unit tests cover the standalone ``filter_relations`` pure function with no
database: schema-only, table-only (exact/glob/bare-glob), combined, and
absent-filter passthrough behavior.
"""

from __future__ import annotations

from canonic.connectors.relation_filter import filter_relations

_RELATIONS: dict[tuple[str, str], str] = {
    ("public", "orders"): "table",
    ("public", "fact_sales"): "table",
    ("finance", "fact_revenue"): "table",
    ("finance", "dim_customer"): "view",
    ("staging", "tmp_orders"): "table",
}


class TestFilterRelations:
    def test_no_filters_passes_through_unchanged(self) -> None:
        assert filter_relations(_RELATIONS, None, None) == _RELATIONS

    def test_empty_lists_pass_through_unchanged(self) -> None:
        assert filter_relations(_RELATIONS, [], []) == _RELATIONS

    def test_schema_only_filter(self) -> None:
        result = filter_relations(_RELATIONS, ["finance"], None)
        assert set(result) == {("finance", "fact_revenue"), ("finance", "dim_customer")}

    def test_schema_filter_is_exact_and_case_sensitive(self) -> None:
        assert filter_relations(_RELATIONS, ["Finance"], None) == {}

    def test_table_exact_match_qualified(self) -> None:
        result = filter_relations(_RELATIONS, None, ["public.orders"])
        assert set(result) == {("public", "orders")}

    def test_table_glob_qualified(self) -> None:
        result = filter_relations(_RELATIONS, None, ["finance.fact_*"])
        assert set(result) == {("finance", "fact_revenue")}

    def test_table_bare_glob_matches_across_schemas(self) -> None:
        result = filter_relations(_RELATIONS, None, ["fact_*"])
        assert set(result) == {("public", "fact_sales"), ("finance", "fact_revenue")}

    def test_combined_schema_then_table_filter(self) -> None:
        # A table pattern must not resurrect a relation from an excluded schema.
        result = filter_relations(_RELATIONS, ["public"], ["fact_*"])
        assert set(result) == {("public", "fact_sales")}

    def test_unmatched_glob_yields_empty(self) -> None:
        assert filter_relations(_RELATIONS, None, ["nonexistent_*"]) == {}

    def test_kind_values_preserved(self) -> None:
        result = filter_relations(_RELATIONS, ["finance"], None)
        assert result[("finance", "dim_customer")] == "view"
