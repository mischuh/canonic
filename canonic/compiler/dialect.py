"""Dialect adapter — transpiles the compiler's neutral AST to target SQL (SPEC-E5-E15 §5).

The compiler builds a dialect-neutral SQLGlot AST; an adapter renders it to a concrete
dialect. Adapter responsibilities: type mapping (internal type set → dialect types),
identifier quoting, ``LIMIT`` injection, and the read-only guarantee. P0 dialect:
PostgreSQL; further dialects plug in behind the same interface.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import cast

import sqlglot
from sqlglot import exp

from canonic.exc import ReadOnlyViolation, UnsupportedDialectError
from canonic.semantic.models import NormalizedType

__all__ = [
    "DIALECT_ADAPTERS",
    "DatabricksDialectAdapter",
    "DialectAdapter",
    "MySQLDialectAdapter",
    "PostgresDialectAdapter",
    "SQLiteDialectAdapter",
    "TYPE_TO_DIALECT",
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

_SNOWFLAKE_TYPE_MAP: dict[NormalizedType, str] = {
    NormalizedType.STRING: "VARCHAR",
    NormalizedType.INT: "BIGINT",
    NormalizedType.DECIMAL: "NUMBER",
    NormalizedType.FLOAT: "DOUBLE",
    NormalizedType.BOOL: "BOOLEAN",
    NormalizedType.DATE: "DATE",
    NormalizedType.TIMESTAMP: "TIMESTAMP_TZ",
    NormalizedType.JSON: "VARIANT",
}

_DATABRICKS_TYPE_MAP: dict[NormalizedType, str] = {
    NormalizedType.STRING: "STRING",
    NormalizedType.INT: "BIGINT",
    NormalizedType.DECIMAL: "DECIMAL",
    NormalizedType.FLOAT: "DOUBLE",
    NormalizedType.BOOL: "BOOLEAN",
    NormalizedType.DATE: "DATE",
    NormalizedType.TIMESTAMP: "TIMESTAMP",
    NormalizedType.JSON: "STRING",
}

_MYSQL_TYPE_MAP: dict[NormalizedType, str] = {
    NormalizedType.STRING: "VARCHAR(255)",
    NormalizedType.INT: "BIGINT",
    NormalizedType.DECIMAL: "DECIMAL(38, 10)",
    NormalizedType.FLOAT: "DOUBLE",
    NormalizedType.BOOL: "BOOLEAN",
    NormalizedType.DATE: "DATE",
    NormalizedType.TIMESTAMP: "DATETIME",
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


def _strip_offset_from_timestamp_cast(node: exp.Expression) -> exp.Expression:
    """Rewrite ``CAST('<iso>' AS TIMESTAMP[TZ])`` into the offset-free wall-clock text.

    Used by SQLite and MySQL. MySQL's ``DATETIME`` has no offset either, and sqlglot renders the
    cast as ``TIMESTAMP('<iso with offset>')``, which MySQL does not parse the offset of.
    SQLite gives an unknown type name NUMERIC affinity, so ``CAST('2025-03-13T23:59:59-04:00'
    AS TIMESTAMPTZ)`` evaluates to the integer ``2025`` and every date column then compares
    as past it. The finality watermark is the one place the compiler emits such a cast.
    SQLite stores timestamps as text without an offset, so the literal becomes
    ``'YYYY-MM-DD HH:MM:SS'`` in the watermark's own timezone.
    """
    if not (
        isinstance(node, exp.Cast)
        and isinstance(node.this, exp.Literal)
        and node.this.is_string
        and node.to.this in (exp.DataType.Type.TIMESTAMP, exp.DataType.Type.TIMESTAMPTZ)
    ):
        return node
    text = node.this.name
    if len(text) < 19 or text[10] not in "T ":
        return node
    return exp.Literal.string(f"{text[:10]} {text[11:19]}")


class SQLiteDialectAdapter(_GenericDialectAdapter):
    """SQLite renderer — has no ordered-set aggregate for percentile queries."""

    def __init__(self) -> None:
        super().__init__("sqlite", _SQLITE_TYPE_MAP)

    def emit(self, ast: exp.Expression, *, limit: int | None = None) -> str:
        ast = ast.transform(_rewrite_interval_arithmetic_for_sqlite)
        ast = ast.transform(_rewrite_date_trunc_for_sqlite)
        ast = ast.transform(_strip_offset_from_timestamp_cast)
        return super().emit(ast, limit=limit)

    def supports_percentile_cont(self) -> bool:
        return False


def _keep_exact_percentile_for_databricks(node: exp.Expression) -> exp.Expression:
    """Keep ``PERCENTILE_CONT(q) WITHIN GROUP (ORDER BY col)`` exact on Databricks.

    sqlglot's Spark and Databricks generators rewrite the ordered-set form into
    ``PERCENTILE_APPROX(col, q)``, which is an approximation and returns different numbers.
    Databricks supports the exact ordered-set aggregate natively, so the node is rebuilt as a
    plain function call the generator leaves alone. The ordering keys lose their explicit
    ``NULLS`` modifier, which the ordered-set syntax does not take and an aggregate ignores.
    """
    if not (isinstance(node, exp.WithinGroup) and isinstance(node.this, exp.PercentileCont)):
        return node
    keys: list[exp.Expression] = []
    for ordered in node.expression.expressions:
        key = ordered.this.copy()
        keys.append(exp.Ordered(this=key, desc=True) if ordered.args.get("desc") else key)
    return exp.WithinGroup(
        this=exp.Anonymous(this="PERCENTILE_CONT", expressions=[node.this.this.copy()]),
        expression=exp.Order(expressions=keys),
    )


class DatabricksDialectAdapter(_GenericDialectAdapter):
    """Databricks renderer, which keeps percentiles exact instead of approximating them."""

    def __init__(self) -> None:
        super().__init__("databricks", _DATABRICKS_TYPE_MAP)

    def emit(self, ast: exp.Expression, *, limit: int | None = None) -> str:
        ast = ast.transform(_keep_exact_percentile_for_databricks)
        return super().emit(ast, limit=limit)


# MySQL has no DATE_TRUNC. sqlglot's own rewrite counts whole units from year 0, which starts
# weeks on Sunday where Postgres and the other engines start them on Monday, so each unit gets an
# explicit template. ``__x`` stands for the truncated expression.
_MYSQL_TRUNC_TEMPLATES: dict[str, str] = {
    "day": "CAST(__x AS DATE)",
    "week": "DATE_SUB(CAST(__x AS DATE), INTERVAL WEEKDAY(__x) DAY)",
    "month": "DATE_SUB(CAST(__x AS DATE), INTERVAL (DAYOFMONTH(__x) - 1) DAY)",
    "quarter": "DATE_ADD(MAKEDATE(YEAR(__x), 1), INTERVAL (QUARTER(__x) - 1) QUARTER)",
    "year": "MAKEDATE(YEAR(__x), 1)",
}


def _rewrite_date_trunc_for_mysql(node: exp.Expression) -> exp.Expression:
    """Rewrite ``DATE_TRUNC(unit, col)`` into MySQL date arithmetic that yields the bucket start."""
    if not isinstance(node, exp.DateTrunc):
        return node
    unit = node.args.get("unit")
    template = _MYSQL_TRUNC_TEMPLATES.get(unit.name.lower() if unit is not None else "")
    if template is None:
        return node
    base = node.this
    return cast(
        "exp.Expression",
        sqlglot.parse_one(template, read="mysql").transform(
            lambda n: base.copy() if isinstance(n, exp.Column) and n.name == "__x" else n
        ),
    )


def _widen_bare_decimal_for_mysql(node: exp.Expression) -> exp.Expression:
    """Give a parameterless ``CAST(x AS DECIMAL)`` an explicit precision and scale.

    MySQL reads a bare ``DECIMAL`` as ``DECIMAL(10, 0)``, which silently rounds every fraction
    away, while Postgres, DuckDB and Snowflake keep them.
    """
    if not (
        isinstance(node, exp.Cast)
        and node.to.this == exp.DataType.Type.DECIMAL
        and not node.to.expressions
    ):
        return node
    return exp.Cast(this=node.this, to=exp.DataType.build("DECIMAL(65, 30)"))


class MySQLDialectAdapter(_GenericDialectAdapter):
    """MySQL renderer. Needs MySQL 8.0 for CTEs and has no ordered-set percentile aggregate."""

    def __init__(self) -> None:
        super().__init__("mysql", _MYSQL_TYPE_MAP)

    def emit(self, ast: exp.Expression, *, limit: int | None = None) -> str:
        ast = ast.transform(_rewrite_date_trunc_for_mysql)
        ast = ast.transform(_strip_offset_from_timestamp_cast)
        ast = ast.transform(_widen_bare_decimal_for_mysql)
        return super().emit(ast, limit=limit)

    def supports_percentile_cont(self) -> bool:
        return False


# Pre-built adapters for the supported query connectors. Redshift is Postgres
# wire-compatible, so it reuses the Postgres type map (spec-drift A1) rather than getting
# its own — but it's a first-class registry entry, not the "any sqlglot dialect works"
# fallback that used to construct it on the fly.
DIALECT_ADAPTERS: dict[str, DialectAdapter] = {
    "postgres": PostgresDialectAdapter(),
    "redshift": _GenericDialectAdapter("redshift", _POSTGRES_TYPE_MAP),
    "duckdb": _GenericDialectAdapter("duckdb", _DUCKDB_TYPE_MAP),
    "snowflake": _GenericDialectAdapter("snowflake", _SNOWFLAKE_TYPE_MAP),
    "databricks": DatabricksDialectAdapter(),
    "mysql": MySQLDialectAdapter(),
    "sqlite": SQLiteDialectAdapter(),
}

# Connection type → sqlglot dialect name when they differ.
TYPE_TO_DIALECT: dict[str, str] = {
    "postgresql": "postgres",
    "pg": "postgres",
}


def adapter_for(dialect: str) -> DialectAdapter:
    """Return the adapter for *dialect*.

    *dialect* is a sqlglot dialect name or a connector ``type`` string. Raises
    :class:`~canonic.exc.UnsupportedDialectError` for anything not in
    ``DIALECT_ADAPTERS`` — compiling a query in the wrong SQL flavor and returning it
    successfully is worse than a clear error, since e.g. ``sl compile`` has no later
    execution-time check that would otherwise catch the mismatch. Only called with a
    query-connector's dialect (see ``core.service._dialect_for_type``, which never
    resolves a dialect for definitions/evidence-only connector types such as dbt/looker/
    metabase/notion/url in the first place), so this never fires for a normally
    configured project.
    """
    normalised = TYPE_TO_DIALECT.get(dialect, dialect)
    adapter = DIALECT_ADAPTERS.get(normalised)
    if adapter is None:
        raise UnsupportedDialectError(dialect, supported=sorted(DIALECT_ADAPTERS))
    return adapter


# SQLite (no ordered-set aggregate) is handled via supports_percentile_cont() — the compiler
# falls back to a CUME_DIST() window-function query. Future: dialects with an approximate
# aggregate instead (e.g. BigQuery/Trino APPROX_QUANTILE) may want a third strategy here.
