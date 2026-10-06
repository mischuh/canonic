"""Dialect adapter — transpiles the compiler's neutral AST to target SQL (SPEC-E5-E15 §5).

The compiler builds a dialect-neutral SQLGlot AST; an adapter renders it to a concrete
dialect. Adapter responsibilities: type mapping (internal type set → dialect types),
identifier quoting, ``LIMIT`` injection, and the read-only guarantee. P0 dialect:
PostgreSQL; further dialects plug in behind the same interface.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, cast

import sqlglot
from sqlglot import exp

from canonic.connectors.readonly import assert_no_writes
from canonic.exc import ReadOnlyViolation, UnsupportedDialectError
from canonic.semantic.models import NormalizedType

if TYPE_CHECKING:
    from collections.abc import Sequence

__all__ = [
    "DIALECT_ADAPTERS",
    "ClickHouseDialectAdapter",
    "DatabricksDialectAdapter",
    "DialectAdapter",
    "MySQLDialectAdapter",
    "PostgresDialectAdapter",
    "SQLiteDialectAdapter",
    "SnowflakeDialectAdapter",
    "TYPE_TO_DIALECT",
    "adapter_for",
]

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

_CLICKHOUSE_TYPE_MAP: dict[NormalizedType, str] = {
    NormalizedType.STRING: "String",
    NormalizedType.INT: "Int64",
    NormalizedType.DECIMAL: "Decimal(38, 10)",
    NormalizedType.FLOAT: "Float64",
    NormalizedType.BOOL: "Bool",
    NormalizedType.DATE: "Date",
    NormalizedType.TIMESTAMP: "DateTime64(3)",
    NormalizedType.JSON: "String",
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

    def json_extract_text(self, column: exp.Expression, segments: Sequence[str]) -> exp.Expression:
        """Extract the key at ``segments`` from a JSON ``column`` as text (AMENDMENT-json-path-dimension).

        The default builds sqlglot's neutral JSON-path node and lets the dialect's generator
        render it. Segments are key names, never a path string, so ``$current_url`` or ``a.b``
        stay one key. Dialects whose rendering is wrong for their JSON column type override this.
        """
        path = exp.JSONPath(
            expressions=[exp.JSONPathRoot(), *(exp.JSONPathKey(this=s) for s in segments)]
        )
        return exp.JSONExtractScalar(this=column, expression=path)

    def json_value(
        self, column: exp.Expression, segments: Sequence[str], value_type: NormalizedType
    ) -> exp.Expression:
        """The key at ``segments`` of a JSON ``column``, cast to ``value_type`` unless it is text."""
        text = self.json_extract_text(column, segments)
        if value_type is NormalizedType.STRING:
            return text
        return exp.Cast(
            this=text, to=exp.DataType.build(self.map_type(value_type), dialect=self.dialect)
        )

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
        assert_no_writes(ast)
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


def _json_key_path(key: str) -> exp.JSONPath:
    return exp.JSONPath(expressions=[exp.JSONPathRoot(), exp.JSONPathKey(this=key)])


class PostgresDialectAdapter(_GenericDialectAdapter):
    """PostgreSQL renderer (the Phase 0 dialect)."""

    def __init__(self) -> None:
        super().__init__("postgres", _POSTGRES_TYPE_MAP)

    def json_extract_text(self, column: exp.Expression, segments: Sequence[str]) -> exp.Expression:
        """Chain ``->`` and a final ``->>``, which work on both ``json`` and ``jsonb``.

        sqlglot's own rendering is ``JSON_EXTRACT_PATH_TEXT``, which has no ``jsonb``
        overload and fails outright on the usual ``jsonb`` column.
        """
        node = column
        for key in segments[:-1]:
            node = exp.JSONExtract(this=node, expression=_json_key_path(key), only_json_types=True)
        return exp.JSONExtractScalar(
            this=node, expression=_json_key_path(segments[-1]), only_json_types=True
        )


class SnowflakeDialectAdapter(_GenericDialectAdapter):
    """Snowflake renderer, reading JSON through ``GET_PATH``, the native ``VARIANT`` accessor."""

    def __init__(self) -> None:
        super().__init__("snowflake", _SNOWFLAKE_TYPE_MAP)

    def json_extract_text(self, column: exp.Expression, segments: Sequence[str]) -> exp.Expression:
        path = "".join(f'["{key}"]' for key in segments)
        get_path = exp.Anonymous(this="GET_PATH", expressions=[column, exp.Literal.string(path)])
        return exp.Cast(this=get_path, to=exp.DataType.build("VARCHAR", dialect="snowflake"))


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

    def json_extract_text(self, column: exp.Expression, segments: Sequence[str]) -> exp.Expression:
        """``JSON_EXTRACT`` works on every SQLite with JSON1, while the ``->>`` operator needs 3.38."""
        path = "$" + "".join(f'."{key}"' for key in segments)
        return exp.Anonymous(this="JSON_EXTRACT", expressions=[column, exp.Literal.string(path)])


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


def _rewrite_percentile_for_clickhouse(node: exp.Expression) -> exp.Expression:
    """Rewrite ``PERCENTILE_CONT(q) WITHIN GROUP (ORDER BY col)`` into ``quantileExactInclusive``.

    ClickHouse has no ordered-set aggregate. ``quantileExactInclusive(q)(col)`` interpolates
    between neighbouring values the same way ``PERCENTILE_CONT`` does and skips NULLs, whereas
    ``quantileExact`` would return an existing value. A descending order is the same percentile
    taken from the other end, so the fraction becomes ``1 - q``. The column is cast to
    ``Float64`` because the function rejects ``Decimal`` arguments, and Postgres returns a double
    there as well. Anything that is not a single ordering key with a literal fraction is left
    alone.
    """
    if not (isinstance(node, exp.WithinGroup) and isinstance(node.this, exp.PercentileCont)):
        return node
    fraction = node.this.this
    keys = node.expression.expressions
    if not (isinstance(fraction, exp.Literal) and not fraction.is_string and len(keys) == 1):
        return node
    ordered = keys[0]
    quantile = float(fraction.name)
    if ordered.args.get("desc"):
        quantile = 1 - quantile
    return exp.ParameterizedAgg(
        this="quantileExactInclusive",
        expressions=[exp.Literal.number(format(quantile, ".15g"))],
        params=[exp.Cast(this=ordered.this.copy(), to=exp.DataType.build("DOUBLE"))],
    )


def _parse_timestamp_literal_for_clickhouse(node: exp.Expression) -> exp.Expression:
    """Rewrite ``CAST('<iso>' AS TIMESTAMP[TZ])`` into ``parseDateTimeBestEffort('<iso>')``.

    The finality watermark is the one place the compiler casts a string literal to a timestamp,
    and it carries a UTC offset. ClickHouse 25.8 LTS turns such a cast into NULL without an
    error, so every ``<= watermark`` filter would silently drop all rows, while 26.x parses it.
    ``parseDateTimeBestEffort`` honours the offset on both and yields the same instant.
    """
    if not (
        isinstance(node, exp.Cast)
        and isinstance(node.this, exp.Literal)
        and node.this.is_string
        and node.to.this in (exp.DataType.Type.TIMESTAMP, exp.DataType.Type.TIMESTAMPTZ)
    ):
        return node
    return exp.Anonymous(this="parseDateTimeBestEffort", expressions=[node.this.copy()])


def _divide_as_float_for_clickhouse(node: exp.Expression) -> exp.Expression:
    """Rewrite ``a / b`` into ``CAST(a AS Float64) / b``.

    A ``Decimal`` division in ClickHouse keeps the scale of the dividend and truncates, so
    ``SUM(repair_cost) / COUNT(*)`` on a ``Decimal(38, 2)`` column turns 2166.666... into 2166.66,
    where Postgres keeps the fraction and SQLite divides as ``REAL``. Every ratio the compiler
    emits is such a division. Integers and floats already divide as ``Float64``, so the cast only
    changes the decimal case.
    """
    if not isinstance(node, exp.Div):
        return node
    numerator = node.this
    if isinstance(numerator, exp.Cast) and numerator.to.this == exp.DataType.Type.DOUBLE:
        return node
    return exp.Div(
        this=exp.Cast(this=numerator.copy(), to=exp.DataType.build("DOUBLE")),
        expression=node.expression.copy(),
    )


def _widen_bare_decimal_for_clickhouse(node: exp.Expression) -> exp.Expression:
    """Give a parameterless ``CAST(x AS Decimal)`` an explicit precision and scale.

    ClickHouse reads a bare ``Decimal`` as ``Decimal(10, 0)``, which rounds every fraction away,
    while Postgres, DuckDB and Snowflake keep them.
    """
    if not (
        isinstance(node, exp.Cast)
        and node.to.this == exp.DataType.Type.DECIMAL
        and not node.to.expressions
    ):
        return node
    return exp.Cast(this=node.this, to=exp.DataType.build("DECIMAL(38, 10)"))


class ClickHouseDialectAdapter(_GenericDialectAdapter):
    """ClickHouse renderer.

    A LEFT JOIN fills unmatched columns with the type default (``0``, empty string) instead of
    NULL unless ``join_use_nulls`` is on, which would turn missing rows into real zeros and break
    every ratio. A query with a join therefore carries ``SETTINGS join_use_nulls = 1`` itself, so
    the compiled SQL gives the same numbers when it is run outside Canonic. The clause is a
    setting change, so the connector runs queries with ``readonly = 2`` rather than ``1``.
    """

    def __init__(self) -> None:
        super().__init__("clickhouse", _CLICKHOUSE_TYPE_MAP)

    def emit(self, ast: exp.Expression, *, limit: int | None = None) -> str:
        ast = ast.transform(_rewrite_percentile_for_clickhouse)
        ast = ast.transform(_parse_timestamp_literal_for_clickhouse)
        ast = ast.transform(_widen_bare_decimal_for_clickhouse)
        ast = ast.transform(_divide_as_float_for_clickhouse)
        if isinstance(ast, (exp.Select, exp.Union)) and ast.find(exp.Join) is not None:
            if isinstance(ast, exp.Union):
                # SETTINGS belongs to a SELECT, so a UNION is wrapped to carry it.
                ast = exp.Select().select(exp.Star()).from_(exp.alias_(ast.subquery(), "_u"))
            ast.set(
                "settings",
                [exp.EQ(this=exp.var("join_use_nulls"), expression=exp.Literal.number(1))],
            )
        return super().emit(ast, limit=limit)


# Pre-built adapters for the supported query connectors. Redshift is Postgres
# wire-compatible, so it reuses the Postgres type map (spec-drift A1) rather than getting
# its own — but it's a first-class registry entry, not the "any sqlglot dialect works"
# fallback that used to construct it on the fly.
DIALECT_ADAPTERS: dict[str, DialectAdapter] = {
    "postgres": PostgresDialectAdapter(),
    "redshift": _GenericDialectAdapter("redshift", _POSTGRES_TYPE_MAP),
    "duckdb": _GenericDialectAdapter("duckdb", _DUCKDB_TYPE_MAP),
    "snowflake": SnowflakeDialectAdapter(),
    "databricks": DatabricksDialectAdapter(),
    "mysql": MySQLDialectAdapter(),
    "clickhouse": ClickHouseDialectAdapter(),
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
