"""Builds the rental example on other engines so one semantic query can run on each.

Authors write ``expr`` in their warehouse's own SQL and canonic does not transpile it, so
the same metric can mean different numbers per dialect. These helpers seed the SQLite
``setup.sql`` into DuckDB and Snowflake (through the in-process ``fakesnow`` emulator) and
point a project copy at it, which lets ``test_dialect_parity.py`` compare the engines'
answers case by case.
"""

from __future__ import annotations

import shutil
from decimal import Decimal
from typing import TYPE_CHECKING, Any

import duckdb
import sqlglot
import yaml
from sqlglot import exp

if TYPE_CHECKING:
    from pathlib import Path

__all__ = [
    "SNOWFLAKE_DATABASE",
    "SNOWFLAKE_PASSWORD_ENV",
    "SNOWFLAKE_SCHEMA",
    "comparable_rows",
    "seed_statements",
    "write_duckdb_rental_project",
    "write_snowflake_rental_project",
]

#: Snowflake database and schema the rental seed lands in. The compiled SQL names tables
#: unqualified, so the connection's ``database``/``schema`` params are what resolve them.
SNOWFLAKE_DATABASE = "RENTAL"
SNOWFLAKE_SCHEMA = "PUBLIC"
SNOWFLAKE_PASSWORD_ENV = "CANONIC_PARITY_SNOWFLAKE_PASSWORD"

_SIG_DIGITS = 9


def seed_statements(setup_sql: str, dialect: str, *, identify: bool = False) -> list[str]:
    """Translate the SQLite seed script to ``dialect``, dropping SQLite-only PRAGMAs.

    ``identify`` quotes every identifier, which Snowflake needs: canonic quotes every name it
    emits, and Snowflake only matches a quoted lower-case name against a table created that way.
    """
    parsed = [s for s in sqlglot.parse(setup_sql, read="sqlite") if s is not None]
    return [
        s.sql(dialect=dialect, identify=identify) for s in parsed if not isinstance(s, exp.Pragma)
    ]


def _copy_project(source: Path, dest: Path, connection: dict[str, Any]) -> None:
    """Copy the rental project and repoint its connections at ``connection``."""
    shutil.copytree(
        source,
        dest,
        dirs_exist_ok=True,
        ignore=shutil.ignore_patterns(".canonic", "*.db", "*.duckdb", "*.wal", ".DS_Store"),
    )
    config_path = dest / "canonic.yaml"
    config = yaml.safe_load(config_path.read_text())
    for entry in config["connections"]:
        entry.update(connection)
    config_path.write_text(yaml.safe_dump(config, sort_keys=False))


def write_duckdb_rental_project(source: Path, dest: Path, setup_sql: str) -> None:
    """Copy the rental project to ``dest`` and run it against a DuckDB file seeded from SQL."""
    db_path = dest / "rental.duckdb"
    _copy_project(source, dest, {"type": "duckdb", "params": {"path": db_path.name}})
    con = duckdb.connect(str(db_path))
    try:
        for statement in seed_statements(setup_sql, "duckdb"):
            con.execute(statement)
    finally:
        con.close()


def write_snowflake_rental_project(source: Path, dest: Path) -> None:
    """Copy the rental project to ``dest`` and point it at a Snowflake account.

    Seeding is not done here: the data lives in the emulator, which only exists while
    ``fakesnow.patch()`` is active, so the test fixture seeds it with :func:`seed_statements`.
    """
    _copy_project(
        source,
        dest,
        {
            "type": "snowflake",
            "params": {
                "account": "fake",
                "user": "canonic",
                "warehouse": "WH",
                "database": SNOWFLAKE_DATABASE,
                "schema": SNOWFLAKE_SCHEMA,
            },
            "credentials_ref": f"env:{SNOWFLAKE_PASSWORD_ENV}",
        },
    )


def _comparable(value: Any) -> Any:
    """Collapse numeric type differences (``Decimal`` vs ``float`` vs ``int``) between engines."""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float, Decimal)):
        return float(f"{float(value):.{_SIG_DIGITS}g}")
    if isinstance(value, str):
        try:
            return float(f"{float(value):.{_SIG_DIGITS}g}")
        except ValueError:
            return value
    return value


def comparable_rows(rows: list[list[Any]]) -> list[list[Any]]:
    """Rows with engine-specific numeric types normalized, order preserved."""
    return [[_comparable(v) for v in row] for row in rows]
