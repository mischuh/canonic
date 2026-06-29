"""Dialect-adapter tests — SPEC-E5-E15 §5 and §9 S6 (read-only & dialect-correct)."""

from __future__ import annotations

import pytest
import sqlglot
from sqlglot import exp

from canon import exc
from canon.compiler.dialect import DIALECT_ADAPTERS, PostgresDialectAdapter, adapter_for
from canon.semantic.models import NormalizedType


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
    assert adapter_for("duckdb").dialect == "duckdb"
    assert adapter_for("sqlite").dialect == "sqlite"


def test_adapter_for_type_aliases() -> None:
    assert adapter_for("postgresql").dialect == "postgres"
    assert adapter_for("pg").dialect == "postgres"


def test_adapter_for_unregistered_sqlglot_dialect() -> None:
    a = adapter_for("bigquery")
    assert a.dialect == "bigquery"


def test_adapter_for_unknown_falls_back_to_postgres() -> None:
    a = adapter_for("nosuchthing")
    assert a.dialect == "postgres"


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
