"""Dialect adapter — transpiles the compiler's neutral AST to target SQL (SPEC-E5-E15 §5).

The compiler builds a dialect-neutral SQLGlot AST; an adapter renders it to a concrete
dialect. Adapter responsibilities: type mapping (internal type set → dialect types),
identifier quoting, ``LIMIT`` injection, and the read-only guarantee. P0 dialect:
PostgreSQL; further dialects plug in behind the same interface.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import cast

from sqlglot import exp

from canonic.exc import ReadOnlyViolation
from canonic.semantic.models import NormalizedType

__all__ = [
    "DIALECT_ADAPTERS",
    "DialectAdapter",
    "PostgresDialectAdapter",
    "SQLiteDialectAdapter",
    "adapter_for",
]

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

    def supports_percentile_cont(self) -> bool:
        """Whether the dialect has a native ordered-set aggregate for percentile queries.

        True means the compiler can emit ``PERCENTILE_CONT(q) WITHIN GROUP (ORDER BY col)``
        directly. Dialects without one (e.g. SQLite) override this to False, which routes
        ``percentile`` recompute_at_grain metrics through a window-function fallback instead
        (SPEC §4.3 open question).
        """
        return True


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


_SQLITE_TRUNC_MODIFIERS: dict[str, str] = {
    "day": "start of day",
    "month": "start of month",
    "year": "start of year",
}


def _rewrite_interval_arithmetic_for_sqlite(node: exp.Expression) -> exp.Expression:
    """Rewrite ``base +/- INTERVAL 'n' unit`` into SQLite's ``DATE(base, '+/-n unit')``.

    SQLite has no ``INTERVAL`` literal, so sqlglot's sqlite generator emits it verbatim
    (invalid SQL). The compiler's neutral AST always represents relative-date arithmetic
    this way regardless of how the filter was originally written, so this rewrite is the
    single place that needs to know SQLite's actual date-modifier syntax.
    """
    if not (isinstance(node, (exp.Add, exp.Sub)) and isinstance(node.expression, exp.Interval)):
        return node
    interval = node.expression
    num = interval.this.name
    unit = interval.args.get("unit")
    unit_name = unit.name.lower() if unit is not None else ""
    sign = "-" if isinstance(node, exp.Sub) else "+"
    modifier = exp.Literal.string(f"{sign}{num} {unit_name}")
    return cast("exp.Expression", exp.func("DATE", node.this, modifier))


def _rewrite_date_trunc_for_sqlite(node: exp.Expression) -> exp.Expression:
    """Rewrite ``DATE_TRUNC(unit, col)`` into SQLite's ``DATE(col, modifier)`` form.

    SQLite has no ``DATE_TRUNC`` function; sqlglot's sqlite generator emits it verbatim
    (invalid SQL). Used both for dimension granularity bucketing and for the SQLite
    ``DATE('now', 'start of ...')`` filter-modifier rewrite in ``_helpers.py``.
    """
    if not isinstance(node, exp.DateTrunc):
        return node
    unit = node.args.get("unit")
    unit_name = unit.name.lower() if unit is not None else ""
    base = node.this
    if unit_name == "week":
        return cast(
            "exp.Expression",
            exp.func("DATE", base, exp.Literal.string("weekday 0"), exp.Literal.string("-6 days")),
        )
    modifier = _SQLITE_TRUNC_MODIFIERS.get(unit_name)
    if modifier is None:
        return node
    return cast("exp.Expression", exp.func("DATE", base, exp.Literal.string(modifier)))


class SQLiteDialectAdapter(_GenericDialectAdapter):
    """SQLite renderer — has no ordered-set aggregate for percentile queries."""

    def __init__(self) -> None:
        super().__init__("sqlite", _SQLITE_TYPE_MAP)

    def emit(self, ast: exp.Expression, *, limit: int | None = None) -> str:
        ast = ast.transform(_rewrite_interval_arithmetic_for_sqlite)
        ast = ast.transform(_rewrite_date_trunc_for_sqlite)
        return super().emit(ast, limit=limit)

    def supports_percentile_cont(self) -> bool:
        return False


# Pre-built adapters for the three supported query connectors.
DIALECT_ADAPTERS: dict[str, DialectAdapter] = {
    "postgres": PostgresDialectAdapter(),
    "duckdb": _GenericDialectAdapter("duckdb", _DUCKDB_TYPE_MAP),
    "sqlite": SQLiteDialectAdapter(),
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


# SQLite (no ordered-set aggregate) is handled via supports_percentile_cont() — the compiler
# falls back to a CUME_DIST() window-function query. Future: dialects with an approximate
# aggregate instead (e.g. BigQuery/Trino APPROX_QUANTILE) may want a third strategy here.
