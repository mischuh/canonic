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

__all__ = ["DIALECT_ADAPTERS", "DialectAdapter", "PostgresDialectAdapter", "adapter_for"]

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

_POSTGRES_TYPE_MAP: dict[NormalizedType, str] = {
    NormalizedType.STRING: "TEXT",
    NormalizedType.INT: "BIGINT",
    NormalizedType.DECIMAL: "NUMERIC",
    NormalizedType.FLOAT: "DOUBLE PRECISION",
    NormalizedType.BOOL: "BOOLEAN",
    NormalizedType.DATE: "DATE",
    NormalizedType.TIMESTAMP: "TIMESTAMPTZ",
    NormalizedType.JSON: "JSONB",
}

_DUCKDB_TYPE_MAP: dict[NormalizedType, str] = {
    NormalizedType.STRING: "TEXT",
    NormalizedType.INT: "BIGINT",
    NormalizedType.DECIMAL: "DECIMAL",
    NormalizedType.FLOAT: "DOUBLE",
    NormalizedType.BOOL: "BOOLEAN",
    NormalizedType.DATE: "DATE",
    NormalizedType.TIMESTAMP: "TIMESTAMPTZ",
    NormalizedType.JSON: "JSON",
}

_SQLITE_TYPE_MAP: dict[NormalizedType, str] = {
    NormalizedType.STRING: "TEXT",
    NormalizedType.INT: "INTEGER",
    NormalizedType.DECIMAL: "REAL",
    NormalizedType.FLOAT: "REAL",
    NormalizedType.BOOL: "INTEGER",
    NormalizedType.DATE: "TEXT",
    NormalizedType.TIMESTAMP: "TEXT",
    NormalizedType.JSON: "TEXT",
}


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


class _GenericDialectAdapter(DialectAdapter):
    """Adapter for any sqlglot-supported dialect, parameterized at construction."""

    def __init__(self, dialect: str, type_map: dict[NormalizedType, str]) -> None:
        self.dialect = dialect
        self._type_map = type_map

    def emit(self, ast: exp.Expression, *, limit: int | None = None) -> str:
        """Render a SELECT (or UNION ALL of SELECTs) to dialect SQL with all identifiers quoted.

        Raises :class:`ReadOnlyViolation` if ``ast`` is anything but a pure, read-only
        SELECT or UNION ALL of SELECTs.
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
        return self._type_map.get(normalized, _POSTGRES_TYPE_MAP[normalized])


class PostgresDialectAdapter(_GenericDialectAdapter):
    """PostgreSQL renderer (the Phase 0 dialect)."""

    def __init__(self) -> None:
        super().__init__("postgres", _POSTGRES_TYPE_MAP)


# Pre-built adapters for the three supported query connectors.
DIALECT_ADAPTERS: dict[str, DialectAdapter] = {
    "postgres": PostgresDialectAdapter(),
    "duckdb": _GenericDialectAdapter("duckdb", _DUCKDB_TYPE_MAP),
    "sqlite": _GenericDialectAdapter("sqlite", _SQLITE_TYPE_MAP),
}

# Connection type → sqlglot dialect name when they differ.
_TYPE_TO_DIALECT: dict[str, str] = {
    "postgresql": "postgres",
    "pg": "postgres",
}


def adapter_for(dialect: str) -> DialectAdapter:
    """Return the adapter for *dialect*, constructing a generic one for unknown dialects.

    *dialect* is a sqlglot dialect name or a connector ``type`` string. Unknown dialects
    fall back to postgres to preserve existing behaviour.
    """
    normalised = _TYPE_TO_DIALECT.get(dialect, dialect)
    if normalised in DIALECT_ADAPTERS:
        return DIALECT_ADAPTERS[normalised]
    # Forward-looking: any connector whose type is a valid sqlglot dialect works.
    try:
        from sqlglot import Dialect

        Dialect.get_or_raise(normalised)
        return _GenericDialectAdapter(normalised, _POSTGRES_TYPE_MAP)
    except (ValueError, AttributeError):
        return DIALECT_ADAPTERS["postgres"]


# Future: add a DialectAdapter.percentile(q, col) method to abstract percentile_cont (Postgres/DuckDB)
# vs approx_quantile (BigQuery/Trino) when multi-dialect support is added (SPEC §4.3 open question).
