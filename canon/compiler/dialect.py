"""Dialect adapter — transpiles the compiler's neutral AST to target SQL (SPEC-E5-E15 §5).

The compiler builds a dialect-neutral SQLGlot AST; an adapter renders it. This is the
issue #11 seam in minimal form: PostgreSQL only, enough for the #10 compiler to emit
read-only, identifier-quoted SQL end-to-end. Type-mapping breadth and further dialects
are completed in #11.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from sqlglot import exp

from canon.exc import ReadOnlyViolation
from canon.semantic.models import NormalizedType

__all__ = ["DIALECT_ADAPTERS", "DialectAdapter", "PostgresDialectAdapter"]


class DialectAdapter(ABC):
    """Renders a neutral AST to a concrete SQL dialect.

    Responsibilities (SPEC-E5-E15 §5): type mapping, identifier quoting, ``LIMIT``
    injection, and the read-only guarantee — a non-``SELECT`` node never reaches emission.
    """

    dialect: str

    @abstractmethod
    def emit(self, ast: exp.Expression, *, limit: int | None = None) -> str:
        """Render ``ast`` to dialect SQL, injecting ``limit`` when provided."""

    @abstractmethod
    def map_type(self, normalized: NormalizedType) -> str:
        """Map a normalized internal type to its dialect type name."""


class PostgresDialectAdapter(DialectAdapter):
    """PostgreSQL renderer (the Phase 0 dialect)."""

    dialect = "postgres"

    _TYPE_MAP: dict[NormalizedType, str] = {
        NormalizedType.STRING: "TEXT",
        NormalizedType.INT: "BIGINT",
        NormalizedType.DECIMAL: "NUMERIC",
        NormalizedType.FLOAT: "DOUBLE PRECISION",
        NormalizedType.BOOL: "BOOLEAN",
        NormalizedType.DATE: "DATE",
        NormalizedType.TIMESTAMP: "TIMESTAMPTZ",
        NormalizedType.JSON: "JSONB",
    }

    def emit(self, ast: exp.Expression, *, limit: int | None = None) -> str:
        """Render a SELECT AST to Postgres SQL with all identifiers quoted.

        Raises :class:`ReadOnlyViolation` if ``ast`` is not a ``SELECT`` — the read-only
        guarantee holds by construction, before any string is produced.
        """
        if not isinstance(ast, exp.Select):
            raise ReadOnlyViolation(f"refusing to emit non-SELECT statement: {type(ast).__name__}")
        if limit is not None:
            ast = ast.limit(limit)
        return ast.sql(dialect=self.dialect, identify=True)

    def map_type(self, normalized: NormalizedType) -> str:
        return self._TYPE_MAP[normalized]


DIALECT_ADAPTERS: dict[str, DialectAdapter] = {"postgres": PostgresDialectAdapter()}
