"""Executes a golden case and canonicalizes the result -- the one code path shared by
assertion and regeneration, so a golden file can never be un-assertable.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any

import sqlglot

if TYPE_CHECKING:
    from canonic.compiler.query import SemanticQuery
    from canonic.core.service import CanonicService

    from .cases import GoldenCase

__all__ = ["RunOutcome", "canonicalize_rows", "canonicalize_value", "pretty_sql", "run_case"]


def canonicalize_value(value: Any) -> Any:
    """Normalize one result cell so equal answers always compare equal.

    ``Decimal`` already round-trips exactly through JSON as a scale-preserving string
    (verified: ``"165.04"``), so it is left alone. ``float`` is rounded to 12 significant
    digits -- far below any real semantic bug (the compiler fixes this suite pins were all
    off by at least a few percent) and far above platform/engine last-bit noise (verified:
    ``avg_revenue`` returns ``8.251999999999999`` raw).
    """
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, float):
        return float(f"{value:.12g}")
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return value


def canonicalize_rows(rows: list[list[Any]], *, ordered: bool) -> list[list[Any]]:
    """Canonicalize every cell, then sort rows unless the case demands emitted order.

    Multi-metric queries emit ``FULL JOIN ... USING (...)``/``UNION ALL`` with no
    ``ORDER BY`` (verified), so row order is not part of the compiler's contract and
    must not make a golden flaky.
    """
    normalized = [[canonicalize_value(v) for v in row] for row in rows]
    if ordered:
        return normalized
    return sorted(normalized, key=lambda r: json.dumps(r, sort_keys=True, default=str))


def pretty_sql(sql: str, dialect: str) -> str:
    """Pretty-print compiled SQL for a reviewable diff.

    Trade-off, stated openly: pretty-printing can mask a pure-whitespace emission
    change. Accepted because a single-line 1000+ character ``WITH ... FULL JOIN ...``
    blob is not reviewable, and semantics are protected by the result artifact
    regardless of SQL formatting.
    """
    parsed = sqlglot.parse_one(sql, dialect=dialect)
    return parsed.sql(dialect=dialect, pretty=True, identify=True) + "\n"


@dataclass(frozen=True, slots=True)
class RunOutcome:
    """What a golden case produces: SQL always; the executed result unless compile_only."""

    sql_text: str
    result_doc: dict[str, Any] | None


def _build_semantic_query(case: GoldenCase) -> SemanticQuery:
    from canonic.compiler.query import SemanticQuery

    return SemanticQuery(**case.query)


async def run_case(case: GoldenCase, service: CanonicService) -> RunOutcome:
    """Compile (and, unless ``compile_only``, execute) one golden case."""
    query = _build_semantic_query(case)

    if case.compile_only:
        compiled = service.compile_query(query)
        return RunOutcome(sql_text=pretty_sql(compiled.sql, compiled.dialect), result_doc=None)

    result = await service.query(query)
    sql_text = pretty_sql(result.compiled.sql, result.compiled.dialect)

    finality_doc = None
    if result.metadata.finality is not None:
        finality_doc = {
            "watermark": result.metadata.finality.watermark,
            "sources_used": result.metadata.finality.sources_used,
            "final_rows": result.metadata.finality.final_rows,
            "provisional_rows": result.metadata.finality.provisional_rows,
        }

    doc: dict[str, Any] = {
        "case": case.id,
        "project": case.project,
        "dialect": result.compiled.dialect,
        "columns": [{"name": c.name, "type": c.type} for c in result.result.columns],
        "rows": canonicalize_rows(result.result.rows, ordered=case.ordered),
        "row_count": len(result.result.rows),
        "resolved": result.metadata.resolved,
        "guardrails_fired": [
            {"id": g.id, "kind": g.kind, "severity": g.severity}
            for g in result.metadata.guardrails_fired
        ],
        "warnings": list(result.metadata.warnings),
        "finality": finality_doc,
        "trust_tier": result.metadata.trust_score.tier if result.metadata.trust_score else None,
    }
    return RunOutcome(sql_text=sql_text, result_doc=doc)
