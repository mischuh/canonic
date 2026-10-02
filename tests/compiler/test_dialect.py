"""Dialect-adapter tests — SPEC-E5-E15 §5 and §9 S6 (read-only & dialect-correct)."""

from __future__ import annotations

import pytest
import sqlglot
from sqlglot import exp

from canonic import exc
from canonic.compiler.dialect import DIALECT_ADAPTERS, PostgresDialectAdapter, adapter_for
from canonic.semantic.models import NormalizedType


@pytest.fixture
def adapter() -> PostgresDialectAdapter:
    return DIALECT_ADAPTERS["postgres"]  # type: ignore[return-value]


def test_select_emits_quoted_postgres(adapter: PostgresDialectAdapter) -> None:
    ast = sqlglot.parse_one("SELECT amount FROM orders")
    sql = adapter.emit(ast)
    assert sql == 'SELECT "amount" FROM "orders"'
    # round-trips through the Postgres parser (S6 AC2)
    assert isinstance(sqlglot.parse_one(sql, dialect="postgres"), exp.Select)


@pytest.mark.parametrize(
    "stmt", ["DELETE FROM orders", "UPDATE orders SET amount = 0", "DROP TABLE orders"]
)
def test_non_select_raises_read_only(adapter: PostgresDialectAdapter, stmt: str) -> None:
    ast = sqlglot.parse_one(stmt)
    with pytest.raises(exc.ReadOnlyViolation) as ei:
        adapter.emit(ast)
    assert ei.value.code is exc.ErrorCode.READ_ONLY_VIOLATION


@pytest.mark.parametrize(
    "stmt",
    [
        # locking SELECT — reads but takes row locks
        "SELECT amount FROM orders FOR UPDATE",
        "SELECT amount FROM orders FOR SHARE",
        # SELECT ... INTO — writes a new relation
        "SELECT amount INTO backup FROM orders",
        # data-modifying CTE — top node is a SELECT but a DELETE/INSERT/UPDATE hides inside
        "WITH t AS (DELETE FROM orders RETURNING *) SELECT * FROM t",
        "WITH t AS (INSERT INTO log VALUES (1) RETURNING *) SELECT * FROM t",
        "WITH t AS (UPDATE orders SET amount = 0 RETURNING *) SELECT * FROM t",
    ],
)
def test_writing_or_locking_select_raises_read_only(
    adapter: PostgresDialectAdapter, stmt: str
) -> None:
    ast = sqlglot.parse_one(stmt, dialect="postgres")
    with pytest.raises(exc.ReadOnlyViolation) as ei:
        adapter.emit(ast)
    assert ei.value.code is exc.ErrorCode.READ_ONLY_VIOLATION


def test_read_only_cte_emits(adapter: PostgresDialectAdapter) -> None:
    # a legitimate read-only CTE must still pass and round-trip through the parser
    ast = sqlglot.parse_one("WITH t AS (SELECT 1 AS n) SELECT n FROM t", dialect="postgres")
    sql = adapter.emit(ast)
    assert isinstance(sqlglot.parse_one(sql, dialect="postgres"), exp.Select)


def test_limit_injected(adapter: PostgresDialectAdapter) -> None:
    ast = sqlglot.parse_one("SELECT amount FROM orders")
    sql = adapter.emit(ast, limit=100)
    assert "LIMIT 100" in sql


def test_map_type_to_postgres(adapter: PostgresDialectAdapter) -> None:
    assert adapter.map_type(NormalizedType.DECIMAL) == "NUMERIC"
    assert adapter.map_type(NormalizedType.TIMESTAMP) == "TIMESTAMPTZ"
    assert adapter.map_type(NormalizedType.JSON) == "JSONB"


def test_registry_exposes_postgres() -> None:
    assert "postgres" in DIALECT_ADAPTERS
    assert DIALECT_ADAPTERS["postgres"].dialect == "postgres"


# --- adapter_for() -----------------------------------------------------------


def test_adapter_for_registered_dialects() -> None:
    assert adapter_for("postgres").dialect == "postgres"
    assert adapter_for("redshift").dialect == "redshift"
    assert adapter_for("duckdb").dialect == "duckdb"
    assert adapter_for("snowflake").dialect == "snowflake"
    assert adapter_for("databricks").dialect == "databricks"
    assert adapter_for("mysql").dialect == "mysql"
    assert adapter_for("sqlite").dialect == "sqlite"


def test_adapter_for_type_aliases() -> None:
    assert adapter_for("postgresql").dialect == "postgres"
    assert adapter_for("pg").dialect == "postgres"


def test_adapter_for_redshift_uses_postgres_type_map() -> None:
    """Redshift is Postgres wire-compatible (spec-drift A1) — same type map, own dialect name."""
    a = adapter_for("redshift")
    assert a.map_type(NormalizedType.DECIMAL) == "NUMERIC"


def test_adapter_for_unregistered_sqlglot_dialect_raises() -> None:
    """A dialect sqlglot happens to know (e.g. bigquery) still has no canonic adapter."""
    with pytest.raises(exc.UnsupportedDialectError) as ei:
        adapter_for("bigquery")
    assert ei.value.code is exc.ErrorCode.CONNECTION_ERROR
    assert "bigquery" in str(ei.value)


def test_adapter_for_unknown_dialect_raises() -> None:
    with pytest.raises(exc.UnsupportedDialectError) as ei:
        adapter_for("nosuchthing")
    assert "postgres" in ei.value.supported


def test_duckdb_adapter_emits_duckdb_interval() -> None:
    """DuckDB uses INTERVAL '3' MONTHS (number separate from unit) not INTERVAL '3 MONTHS'."""
    neutral = exp.Sub(
        this=exp.CurrentDate(),
        expression=exp.Interval(this=exp.Literal.string("3"), unit=exp.Var(this="MONTHS")),
    )
    ast = exp.select(neutral)
    duckdb_sql = adapter_for("duckdb").emit(ast)
    postgres_sql = adapter_for("postgres").emit(ast)
    # DuckDB form: INTERVAL '3' MONTHS — number and unit are separate tokens
    assert "INTERVAL '3' MONTHS" in duckdb_sql
    # Postgres form: INTERVAL '3 MONTHS' — unit is part of the quoted string
    assert "INTERVAL '3 MONTHS'" in postgres_sql


def test_duckdb_adapter_read_only_guards_still_apply() -> None:
    ast = sqlglot.parse_one("DELETE FROM orders")
    with pytest.raises(exc.ReadOnlyViolation):
        adapter_for("duckdb").emit(ast)


def test_duckdb_type_map() -> None:
    a = adapter_for("duckdb")
    assert a.map_type(NormalizedType.DECIMAL) == "DECIMAL"
    assert a.map_type(NormalizedType.JSON) == "JSON"


def test_sqlite_type_map() -> None:
    a = adapter_for("sqlite")
    assert a.map_type(NormalizedType.INT) == "INTEGER"
    assert a.map_type(NormalizedType.BOOL) == "INTEGER"


def test_snowflake_type_map() -> None:
    a = adapter_for("snowflake")
    assert a.map_type(NormalizedType.STRING) == "VARCHAR"
    assert a.map_type(NormalizedType.DECIMAL) == "NUMBER"
    assert a.map_type(NormalizedType.TIMESTAMP) == "TIMESTAMP_TZ"
    assert a.map_type(NormalizedType.JSON) == "VARIANT"


def test_snowflake_adapter_emits_native_sql() -> None:
    """Date truncation, interval arithmetic, TIMESTAMPTZ casts and ordered-set percentiles all
    transpile to valid Snowflake SQL, and identifiers keep the case they were written in."""
    import sqlglot

    neutral = sqlglot.parse_one(
        "SELECT DATE_TRUNC('month', o.created_at) AS m, CAST(o.ts AS TIMESTAMPTZ) AS t, "
        "PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY o.amount) AS p50 "
        "FROM orders AS o WHERE o.created_at >= CURRENT_DATE - INTERVAL '3' MONTH",
        dialect="postgres",
    )
    sql = adapter_for("snowflake").emit(neutral, limit=10)
    assert 'DATE_TRUNC(\'MONTH\', "o"."created_at")' in sql
    assert 'CAST("o"."ts" AS TIMESTAMPTZ)' in sql
    assert "INTERVAL '3 MONTH'" in sql
    assert 'PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY "o"."amount")' in sql
    assert sql.endswith("LIMIT 10")
    assert adapter_for("snowflake").supports_percentile_cont()


def test_databricks_type_map() -> None:
    a = adapter_for("databricks")
    assert a.map_type(NormalizedType.STRING) == "STRING"
    assert a.map_type(NormalizedType.INT) == "BIGINT"
    assert a.map_type(NormalizedType.TIMESTAMP) == "TIMESTAMP"
    assert a.map_type(NormalizedType.JSON) == "STRING"


def test_databricks_adapter_emits_native_sql_with_exact_percentile() -> None:
    """Identifiers use backticks and the percentile stays ``PERCENTILE_CONT``.

    sqlglot's own Databricks generator turns the ordered-set form into ``PERCENTILE_APPROX``,
    an approximation that would change the numbers, so the adapter must not let that through.
    """
    import sqlglot

    neutral = sqlglot.parse_one(
        "SELECT DATE_TRUNC('month', o.created_at) AS m, "
        "PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY o.amount) AS p50 "
        "FROM orders AS o WHERE o.created_at >= CURRENT_DATE - INTERVAL '3' MONTH",
        dialect="postgres",
    )
    sql = adapter_for("databricks").emit(neutral, limit=10)
    assert "DATE_TRUNC('MONTH', `o`.`created_at`)" in sql
    assert "PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY `o`.`amount`)" in sql
    assert "PERCENTILE_APPROX" not in sql
    assert "INTERVAL '3' MONTH" in sql
    assert sql.endswith("LIMIT 10")
    assert adapter_for("databricks").supports_percentile_cont()


def test_mysql_type_map() -> None:
    a = adapter_for("mysql")
    assert a.map_type(NormalizedType.STRING) == "VARCHAR(255)"
    assert a.map_type(NormalizedType.INT) == "BIGINT"
    assert a.map_type(NormalizedType.DECIMAL) == "DECIMAL(38, 10)"
    assert a.map_type(NormalizedType.TIMESTAMP) == "DATETIME"
    assert a.map_type(NormalizedType.JSON) == "JSON"


def test_mysql_adapter_quotes_with_backticks_and_limits() -> None:
    sql = adapter_for("mysql").emit(sqlglot.parse_one("SELECT amount FROM orders"), limit=10)
    assert sql == "SELECT `amount` FROM `orders` LIMIT 10"


def test_mysql_adapter_has_no_exact_percentile() -> None:
    """MySQL has no ordered-set aggregate, so the compiler takes the CUME_DIST fallback."""
    assert not adapter_for("mysql").supports_percentile_cont()


@pytest.mark.parametrize(
    ("unit", "expected"),
    [
        ("day", "CAST(`o`.`d` AS DATE)"),
        ("week", "DATE_SUB(CAST(`o`.`d` AS DATE), INTERVAL (WEEKDAY(`o`.`d`)) DAY)"),
        ("month", "DATE_SUB(CAST(`o`.`d` AS DATE), INTERVAL (DAYOFMONTH(`o`.`d`) - 1) DAY)"),
        (
            "quarter",
            "DATE_ADD(MAKEDATE(YEAR(`o`.`d`), 1), INTERVAL (QUARTER(`o`.`d`) - 1) QUARTER)",
        ),
        ("year", "MAKEDATE(YEAR(`o`.`d`), 1)"),
    ],
)
def test_mysql_date_trunc_buckets_to_the_start_of_the_period(unit: str, expected: str) -> None:
    """sqlglot's own DATE_TRUNC rewrite counts units from year 0, so a week would start on Sunday."""
    trunc = exp.func("DATE_TRUNC", exp.Literal.string(unit), exp.column("d", table="o"))
    sql = adapter_for("mysql").emit(exp.select(trunc).from_("o"))
    assert sql == f"SELECT {expected} FROM `o`"


def test_mysql_watermark_cast_keeps_wall_clock_text() -> None:
    """MySQL's ``DATETIME`` has no offset, so the watermark literal must not carry one."""
    neutral = sqlglot.parse_one(
        "SELECT 1 FROM t WHERE d <= CAST('2025-03-13T23:59:59-04:00' AS TIMESTAMPTZ)",
        dialect="postgres",
    )
    sql = adapter_for("mysql").emit(neutral)
    assert "'2025-03-13 23:59:59'" in sql
    assert "-04:00" not in sql


def test_mysql_bare_decimal_cast_keeps_its_fraction() -> None:
    """A bare ``DECIMAL`` is ``DECIMAL(10, 0)`` in MySQL and would round every fraction away."""
    neutral = sqlglot.parse_one("SELECT CAST(x AS NUMERIC), CAST(y AS NUMERIC(10, 2)) FROM t")
    sql = adapter_for("mysql").emit(neutral)
    assert "CAST(`x` AS DECIMAL(65, 30))" in sql
    assert "CAST(`y` AS DECIMAL(10, 2))" in sql


def test_mysql_adapter_refuses_writes() -> None:
    with pytest.raises(exc.ReadOnlyViolation):
        adapter_for("mysql").emit(sqlglot.parse_one("DELETE FROM t", dialect="mysql"))


def test_sqlite_watermark_cast_keeps_wall_clock_text() -> None:
    """A ``CAST('<iso>' AS TIMESTAMPTZ)`` watermark must not collapse to an integer on SQLite.

    SQLite gives an unknown type name NUMERIC affinity, so the cast turns the ISO string
    into ``2025`` and every date column then compares as past the watermark. The adapter
    rewrites it to the offset-free wall-clock text SQLite stores timestamps as.
    """
    import sqlite3

    ast = sqlglot.parse_one(
        "SELECT d FROM t WHERE d <= CAST('2025-03-13T23:59:59-04:00' AS TIMESTAMPTZ)",
        dialect="postgres",
    )
    sql = adapter_for("sqlite").emit(ast)
    assert "TIMESTAMPTZ" not in sql
    con = sqlite3.connect(":memory:")
    con.execute("CREATE TABLE t (d TEXT)")
    con.executemany(
        "INSERT INTO t VALUES (?)",
        [("2025-03-10",), ("2025-03-13 12:00:00",), ("2025-03-14",)],
    )
    assert [r[0] for r in con.execute(sql)] == ["2025-03-10", "2025-03-13 12:00:00"]
