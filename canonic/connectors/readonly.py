"""Parse-level read-only guard (SPEC-E2 §3.2, SPEC-E7/E8 §7 S5).

First line of defense: rejects any non-SELECT or multi-statement SQL *before*
a DB connection is opened. The compiler's dialect adapter (canonic/compiler/dialect.py)
adds a deeper AST-level guard as defense-in-depth.
"""

from __future__ import annotations

import sqlglot
from sqlglot import expressions as exp
from sqlglot.errors import ParseError

from canonic.exc import ReadOnlyViolation


def assert_read_only(sql: str) -> None:
    """Raise ReadOnlyViolation unless sql is exactly one read-only SELECT/UNION.

    Rejects: unparseable SQL, multiple statements (';' split → len != 1),
    and any root node that is not Select/Union. Never opens a connection.
    """
    try:
        statements = [s for s in sqlglot.parse(sql, read="postgres") if s is not None]
    except ParseError as exc:
        raise ReadOnlyViolation(f"could not parse SQL as read-only: {exc}") from exc
    if len(statements) != 1:
        raise ReadOnlyViolation(f"exactly one statement is allowed, got {len(statements)}")
    stmt = statements[0]
    if not isinstance(stmt, (exp.Select, exp.Union)):
        raise ReadOnlyViolation(f"only SELECT statements are permitted, got {type(stmt).__name__}")
