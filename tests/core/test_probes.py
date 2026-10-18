"""Dropping the compiler's probe columns from an executed result."""

from __future__ import annotations

from canonic.compiler.result import PROBE_ZERO_DENOMINATOR, CompileResult, ProbeColumn
from canonic.connectors.base import ResultColumn, ResultSet
from canonic.core.probes import strip_probes

_PROBE = ProbeColumn("_p", PROBE_ZERO_DENOMINATOR, "share")


def _compiled(*probes: ProbeColumn) -> CompileResult:
    return CompileResult(sql="SELECT 1", dialect="postgres", resolved={}, probe_columns=probes)


def _result(*names: str, rows: list[list[object]]) -> ResultSet:
    return ResultSet(
        columns=[ResultColumn(name=n, type="int") for n in names],
        rows=rows,
        truncated=True,
        bytes_scanned=7,
    )


def test_probe_column_is_counted_and_removed() -> None:
    result = _result("d", "share", "_p", rows=[["x", 1.0, 0], ["y", None, 1], ["z", None, 1]])

    stripped, counts = strip_probes(_compiled(_PROBE), result)

    assert [c.name for c in stripped.columns] == ["d", "share"]
    assert stripped.rows == [["x", 1.0], ["y", None], ["z", None]]
    assert counts == {(PROBE_ZERO_DENOMINATOR, "share"): 2}


def test_result_metadata_is_kept() -> None:
    stripped, _ = strip_probes(_compiled(_PROBE), _result("share", "_p", rows=[[1, 0]]))

    assert stripped.truncated is True
    assert stripped.bytes_scanned == 7


def test_null_flag_counts_as_no_flag() -> None:
    _, counts = strip_probes(_compiled(_PROBE), _result("share", "_p", rows=[[1, None], [2, 1]]))

    assert counts == {(PROBE_ZERO_DENOMINATOR, "share"): 1}


def test_boolean_and_decimal_flags_are_read() -> None:
    from decimal import Decimal

    _, counts = strip_probes(
        _compiled(_PROBE), _result("share", "_p", rows=[[1, True], [2, Decimal(1)], [3, False]])
    )

    assert counts == {(PROBE_ZERO_DENOMINATOR, "share"): 2}


def test_result_without_the_probe_column_is_left_alone() -> None:
    """A connector that returns a fixed result set must keep working."""
    result = _result("share", rows=[[1]])

    stripped, counts = strip_probes(_compiled(_PROBE), result)

    assert stripped is result
    assert counts == {}


def test_query_without_probes_returns_the_result_itself() -> None:
    result = _result("share", "_p", rows=[[1, 1]])

    stripped, counts = strip_probes(_compiled(), result)

    assert stripped is result
    assert counts == {}
