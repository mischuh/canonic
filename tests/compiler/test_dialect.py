"""Dialect-adapter tests — SPEC-E5-E15 §5 and §9 S6 (read-only & dialect-correct)."""

from __future__ import annotations

import pytest
import sqlglot
from sqlglot import exp

from canon import exc
from canon.compiler.dialect import DIALECT_ADAPTERS, PostgresDialectAdapter
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
