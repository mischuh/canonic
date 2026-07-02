"""Tests for FinalityOut row counts in QueryMetadata (SPEC-E5-E15 stage 8, AC2)."""

from __future__ import annotations

from canonic.compiler.result import CompileResult, FinalityMetadata, FiredGuardrail, SourceFreshness
from canonic.connectors.base import ResultColumn, ResultSet
from canonic.core.models import QueryMetadata


def _make_compile_result(with_finality: bool = True) -> CompileResult:
    finality = (
        FinalityMetadata(
            watermark="2026-06-12T23:59:59-04:00",
            sources_used=["orders", "orders_rt"],
            result_flag="per_row",
        )
        if with_finality
        else None
    )
    return CompileResult(
        sql="SELECT 1",
        dialect="postgres",
        resolved={"revenue": "orders.total_revenue"},
        guardrails_fired=[FiredGuardrail(id="g1", kind="mandatory_filter")],
        freshness=[SourceFreshness(source="orders", last_validated_at=None, stale=False)],
        warnings=[],
        finality=finality,
    )


def _make_result_set(is_final_values: list[bool]) -> ResultSet:
    columns = [
        ResultColumn(name="order_date", type="timestamp"),
        ResultColumn(name="total_revenue", type="decimal"),
        ResultColumn(name="is_final", type="bool"),
    ]
    rows = [[f"2026-06-{10 + i}", 100.0, v] for i, v in enumerate(is_final_values)]
    return ResultSet(columns=columns, rows=rows)


class TestFinalityOutRowCounts:
    def test_row_counts_tallied_from_result_set(self) -> None:
        compiled = _make_compile_result(with_finality=True)
        result = _make_result_set([True, True, True, True, True, True, False])

        meta = QueryMetadata.from_compile_result(compiled, result=result)

        assert meta.finality is not None
        assert meta.finality.final_rows == 6
        assert meta.finality.provisional_rows == 1

    def test_watermark_and_sources_carried_through(self) -> None:
        compiled = _make_compile_result(with_finality=True)
        result = _make_result_set([True, False])

        meta = QueryMetadata.from_compile_result(compiled, result=result)

        assert meta.finality is not None
        assert meta.finality.watermark == "2026-06-12T23:59:59-04:00"
        assert meta.finality.sources_used == ["orders", "orders_rt"]

    def test_no_result_set_gives_none_counts(self) -> None:
        compiled = _make_compile_result(with_finality=True)

        meta = QueryMetadata.from_compile_result(compiled, result=None)

        assert meta.finality is not None
        assert meta.finality.final_rows is None
        assert meta.finality.provisional_rows is None

    def test_no_finality_rule_gives_none_finality(self) -> None:
        compiled = _make_compile_result(with_finality=False)
        result = _make_result_set([True, False])

        meta = QueryMetadata.from_compile_result(compiled, result=result)

        assert meta.finality is None

    def test_result_without_is_final_column_gives_none_counts(self) -> None:
        compiled = _make_compile_result(with_finality=True)
        result = ResultSet(
            columns=[ResultColumn(name="total_revenue", type="decimal")],
            rows=[[100.0]],
        )

        meta = QueryMetadata.from_compile_result(compiled, result=result)

        assert meta.finality is not None
        assert meta.finality.final_rows is None
        assert meta.finality.provisional_rows is None
