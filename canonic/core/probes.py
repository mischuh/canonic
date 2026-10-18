"""Read and drop the probe columns the compiler added to a statement."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from canonic.compiler.result import PROBE_LATEST_PERIOD
from canonic.connectors.base import ResultSet

if TYPE_CHECKING:
    from canonic.compiler.result import CompileResult

__all__ = ["ProbeValues", "strip_probes"]

#: ``(kind, subject)`` to what the probe found: a row count for a flag, the latest value for
#: a ``latest_period`` probe.
ProbeValues = dict[tuple[str, str], Any]


def _latest(values: list[Any]) -> Any:
    """The greatest of ``values``, or ``None`` if there is none or they cannot be compared."""
    try:
        return max(values) if values else None
    except TypeError:
        return None


def strip_probes(compiled: CompileResult, result: ResultSet) -> tuple[ResultSet, ProbeValues]:
    """The result without the compiler's probe columns, and what each probe found.

    A flag column is counted, and a ``latest_period`` column reduces to its greatest value over
    the rows. A probe column that the result does not carry is skipped rather than treated as an
    error, so a connector that returns a fixed result set still works. A ``NULL`` is no flag
    and no value.
    """
    if not compiled.probe_columns:
        return result, {}
    names = [column.name for column in result.columns]
    counts: ProbeValues = {}
    hidden: set[int] = set()
    for probe in compiled.probe_columns:
        if probe.column not in names:
            continue
        index = names.index(probe.column)
        hidden.add(index)
        cells = [row[index] for row in result.rows if row[index] is not None]
        counts[(probe.kind, probe.subject)] = (
            _latest(cells)
            if probe.kind == PROBE_LATEST_PERIOD
            else sum(1 for cell in cells if int(cell) != 0)
        )
    if not hidden:
        return result, counts
    keep = [i for i in range(len(names)) if i not in hidden]
    return (
        ResultSet(
            columns=[result.columns[i] for i in keep],
            rows=[[row[i] for i in keep] for row in result.rows],
            truncated=result.truncated,
            bytes_scanned=result.bytes_scanned,
        ),
        counts,
    )
