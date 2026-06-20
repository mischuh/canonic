"""Dialect adapter — transpiles the compiler's neutral AST to target SQL (SPEC-E5-E15 §5).

The compiler builds a dialect-neutral SQLGlot AST; an adapter renders it to a concrete
dialect. Adapter responsibilities: type mapping (internal type set → dialect types),
identifier quoting, ``LIMIT`` injection, and the read-only guarantee. P0 dialect:
PostgreSQL; further dialects plug in behind the same interface.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from sqlglot import exp

from canon.exc import ReadOnlyViolation
from canon.semantic.models import NormalizedType

__all__ = ["DIALECT_ADAPTERS", "DialectAdapter", "PostgresDialectAdapter"]

# DML/DDL nodes that may never appear anywhere in the AST — including inside a CTE
# (Postgres permits data-modifying statements in ``WITH``). Catching them by class
# covers ``WITH t AS (DELETE … RETURNING *) SELECT …`` and friends.
_WRITE_NODES: tuple[type[exp.Expression], ...] = (
    exp.Insert,
    exp.Update,
    exp.Delete,
    exp.Merge,
    exp.Create,
    exp.Drop,
    exp.Alter,
    exp.TruncateTable,
)


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
        """Render a SELECT (or UNION ALL of SELECTs) to Postgres SQL with all identifiers quoted.

        Raises :class:`ReadOnlyViolation` if ``ast`` is anything but a pure, read-only
        SELECT or UNION ALL of SELECTs. The guarantee holds by construction — every
        write/lock path is rejected before any string is produced.
        """
        if not isinstance(ast, (exp.Select, exp.Union)):
            raise ReadOnlyViolation(f"refusing to emit non-SELECT statement: {type(ast).__name__}")
        if (write := ast.find(*_WRITE_NODES)) is not None:
            raise ReadOnlyViolation(
                f"refusing to emit data-modifying statement: {type(write).__name__}"
            )
        if ast.find(exp.Into) is not None:
            raise ReadOnlyViolation("refusing to emit SELECT ... INTO (writes a new relation)")
        if isinstance(ast, exp.Select) and ast.args.get("locks"):
            raise ReadOnlyViolation("refusing to emit locking SELECT (FOR UPDATE / FOR SHARE)")
        if limit is not None:
            # Wrap a UNION in a subquery so LIMIT applies to the combined result.
            if isinstance(ast, exp.Union):
                from sqlglot import exp as _exp

                ast = (
                    _exp.Select()
                    .select(_exp.Star())
                    .from_(_exp.alias_(ast.subquery(), "_u"))
                    .limit(limit)
                )
            else:
                ast = ast.limit(limit)
        return ast.sql(dialect=self.dialect, identify=True)

    def map_type(self, normalized: NormalizedType) -> str:
        return self._TYPE_MAP[normalized]


DIALECT_ADAPTERS: dict[str, DialectAdapter] = {"postgres": PostgresDialectAdapter()}
