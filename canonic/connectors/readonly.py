"""Parse-level read-only guard (SPEC-E2 §3.2, SPEC-E7/E8 §7 S5).

First line of defense: rejects any non-SELECT or multi-statement SQL *before*
a DB connection is opened. The compiler's dialect adapter (canonic/compiler/dialect.py)
applies the same AST check to compiled SQL via :func:`assert_no_writes`.
"""

from __future__ import annotations

import sqlglot
from sqlglot import expressions as exp
from sqlglot.errors import ParseError

from canonic.exc import ReadOnlyViolation

# DML/DDL nodes that may never appear anywhere in the AST, including inside a CTE
# (Postgres permits data-modifying statements in ``WITH``). Catching them by class
# covers ``WITH t AS (DELETE ... RETURNING *) SELECT ...`` and friends.
WRITE_NODES: tuple[type[exp.Expression], ...] = (
    exp.Insert,
    exp.Update,
    exp.Delete,
    exp.Merge,
    exp.Create,
    exp.Drop,
    exp.Alter,
    exp.TruncateTable,
)


def assert_no_writes(ast: exp.Expression) -> None:
    """Raise ReadOnlyViolation if ``ast`` contains a write, a new relation or a row lock.

    Walks the whole tree, so a data-modifying CTE or subquery is caught as well as a
    top-level statement. Rejects DML/DDL nodes, ``SELECT ... INTO`` (creates a relation)
    and locking reads (``FOR UPDATE`` / ``FOR SHARE``) at any depth.
    """
    if (write := ast.find(*WRITE_NODES)) is not None:
        raise ReadOnlyViolation(f"refusing data-modifying statement: {type(write).__name__}")
    if ast.find(exp.Into) is not None:
        raise ReadOnlyViolation("refusing SELECT ... INTO (writes a new relation)")
    if ast.find(exp.Lock) is not None:
        raise ReadOnlyViolation("refusing locking SELECT (FOR UPDATE / FOR SHARE)")


def assert_read_only(sql: str, dialect: str = "postgres") -> None:
    """Raise ReadOnlyViolation unless sql is exactly one read-only SELECT/UNION.

    Rejects: unparseable SQL, multiple statements (';' split → len != 1),
    any root node that is not Select/Union, and any write, ``SELECT ... INTO`` or
    locking read nested anywhere in the tree (see :func:`assert_no_writes`).
    Never opens a connection.

    ``dialect`` is the sqlglot dialect used to parse ``sql``. It defaults to Postgres, which
    covers every connector except those whose native syntax Postgres cannot parse
    (e.g. Snowflake's ``col:path::type`` variant access).
    """
    try:
        statements = [s for s in sqlglot.parse(sql, read=dialect) if s is not None]
    except ParseError as exc:
        raise ReadOnlyViolation(f"could not parse SQL as read-only: {exc}") from exc
    if len(statements) != 1:
        raise ReadOnlyViolation(f"exactly one statement is allowed, got {len(statements)}")
    stmt = statements[0]
    if not isinstance(stmt, (exp.Select, exp.Union)):
        raise ReadOnlyViolation(f"only SELECT statements are permitted, got {type(stmt).__name__}")
    assert_no_writes(stmt)
