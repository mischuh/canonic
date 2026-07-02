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
