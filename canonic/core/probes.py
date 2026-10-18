"""Read and drop the probe columns the compiler added to a statement."""

from __future__ import annotations

from typing import TYPE_CHECKING

from canonic.connectors.base import ResultSet

if TYPE_CHECKING:
    from canonic.compiler.result import CompileResult

__all__ = ["ProbeCounts", "strip_probes"]

#: ``(kind, subject)`` to the number of result rows the probe flagged.
ProbeCounts = dict[tuple[str, str], int]


def strip_probes(compiled: CompileResult, result: ResultSet) -> tuple[ResultSet, ProbeCounts]:
    """The result without the compiler's probe columns, and what each probe counted.

    A probe column that the result does not carry is skipped rather than treated as an error,
    so a connector that returns a fixed result set still works. A flag that is ``NULL`` counts
    as no flag.
    """
    if not compiled.probe_columns:
        return result, {}
    names = [column.name for column in result.columns]
    counts: ProbeCounts = {}
    hidden: set[int] = set()
    for probe in compiled.probe_columns:
        if probe.column not in names:
            continue
        index = names.index(probe.column)
        hidden.add(index)
        counts[(probe.kind, probe.subject)] = sum(
            1 for row in result.rows if row[index] is not None and int(row[index]) != 0
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
