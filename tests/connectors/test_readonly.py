"""Tests for the parse-level read-only guard (GH-12).

Unit tests cover the standalone ``assert_read_only`` guard with no database:
SELECT/CTE/UNION pass; any non-SELECT, multi-statement, or unparseable SQL is
rejected with ``ReadOnlyViolation`` before a connection is ever opened.
"""

from __future__ import annotations

import pytest

from canonic.connectors.readonly import assert_read_only
from canonic.exc import ErrorCode, ReadOnlyViolation


class TestAssertReadOnly:
    @pytest.mark.parametrize(
        "sql",
        [
            "SELECT 1",
            "SELECT a, b FROM analytics.fct_orders WHERE a > 1",
            "WITH x AS (SELECT 1 AS a) SELECT a FROM x",
            "SELECT 1 UNION SELECT 2",
        ],
    )
    def test_select_allowed(self, sql: str) -> None:
        assert_read_only(sql)  # must not raise

    @pytest.mark.parametrize(
        "sql",
        [
            "INSERT INTO t VALUES (1)",
            "UPDATE t SET a = 1",
            "DELETE FROM t",
            "DROP TABLE t",
            "CREATE TABLE t (a int)",
            "TRUNCATE t",
            "SELECT 1; SELECT 2",
            "SELECT 1; DROP TABLE t",
            "this is not sql ((",
        ],
    )
    def test_non_select_rejected(self, sql: str) -> None:
        with pytest.raises(ReadOnlyViolation) as ei:
            assert_read_only(sql)
        assert ei.value.code is ErrorCode.READ_ONLY_VIOLATION


class TestSnowflakeDialect:
    _VARIANT_PATH = "SELECT payload:user.id::string AS uid FROM events"

    def test_variant_path_needs_snowflake_dialect(self) -> None:
        with pytest.raises(ReadOnlyViolation):
            assert_read_only(self._VARIANT_PATH)
        assert_read_only(self._VARIANT_PATH, dialect="snowflake")  # must not raise

    @pytest.mark.parametrize("sql", ["DELETE FROM t", "DROP TABLE t", "SELECT 1; SELECT 2"])
    def test_writes_still_rejected(self, sql: str) -> None:
        with pytest.raises(ReadOnlyViolation):
            assert_read_only(sql, dialect="snowflake")


class TestDatabricksDialect:
    _VARIANT_PATH = "SELECT payload:user.id AS uid FROM `main`.`raw`.`events`"

    def test_backticks_and_variant_path_need_databricks_dialect(self) -> None:
        with pytest.raises(ReadOnlyViolation):
            assert_read_only(self._VARIANT_PATH)
        assert_read_only(self._VARIANT_PATH, dialect="databricks")  # must not raise

    @pytest.mark.parametrize("sql", ["DELETE FROM t", "DROP TABLE t", "SELECT 1; SELECT 2"])
    def test_writes_still_rejected(self, sql: str) -> None:
        with pytest.raises(ReadOnlyViolation):
            assert_read_only(sql, dialect="databricks")


class TestMySQLDialect:
    def test_backticks_need_mysql_dialect(self) -> None:
        sql = "SELECT `order`.`id` FROM `shop`.`order`"
        with pytest.raises(ReadOnlyViolation):
            assert_read_only(sql)
        assert_read_only(sql, dialect="mysql")  # must not raise

    @pytest.mark.parametrize(
        "sql",
        [
            "DELETE FROM t",
            "DROP TABLE t",
            "SELECT 1; SELECT 2",
            "SELECT * FROM t INTO OUTFILE '/tmp/x'",
        ],
    )
    def test_writes_still_rejected(self, sql: str) -> None:
        with pytest.raises(ReadOnlyViolation):
            assert_read_only(sql, dialect="mysql")


class TestClickHouseDialect:
    def test_clickhouse_only_syntax_needs_clickhouse_dialect(self) -> None:
        sql = "SELECT quantileExactInclusive(0.5)(x) FROM t SETTINGS join_use_nulls = 1"
        with pytest.raises(ReadOnlyViolation):
            assert_read_only(sql)
        assert_read_only(sql, dialect="clickhouse")  # must not raise

    @pytest.mark.parametrize(
        "sql",
        [
            "INSERT INTO t VALUES (1)",
            "DROP TABLE t",
            "ALTER TABLE t DELETE WHERE 1",
            "SELECT 1; SELECT 2",
            "SELECT * FROM t INTO OUTFILE '/tmp/x'",
        ],
    )
    def test_writes_still_rejected(self, sql: str) -> None:
        with pytest.raises(ReadOnlyViolation):
            assert_read_only(sql, dialect="clickhouse")
