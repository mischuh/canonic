"""Shared live-schema discovery and interactive schema/table selection prompts.

Used by ``canonic setup`` (narrowing a brand-new connection) and ``canonic ingest
add-tables`` (widening an existing connection's table filter) so both commands
present the identical numbered-list / index-range / glob picker.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, cast

import typer
from rich.table import Table

from canonic.cli.commands import _console
from canonic.connectors.factory import default_factory
from canonic.exc import ConnectionError, CredentialError

if TYPE_CHECKING:
    from canonic.config import Connection
    from canonic.connectors.base import RelationSchema, SchemaIntrospectable

__all__ = [
    "discover_relations",
    "introspect_connection",
    "is_index_range",
    "parse_index_ranges",
    "parse_table_tokens",
    "prompt_select_schemas",
    "prompt_select_tables",
]


async def introspect_connection(conn: Connection) -> list[RelationSchema]:
    connector = default_factory.create(conn)
    try:
        return list(await cast("SchemaIntrospectable", connector).introspect_schema())
    finally:
        await connector.aclose()


def discover_relations(conn: Connection) -> list[RelationSchema] | None:
    """Introspect conn (unfiltered) to discover what schemas/tables exist."""
    try:
        return asyncio.run(introspect_connection(conn))
    except (CredentialError, ConnectionError) as exc:
        _console.print(f"[yellow]schema discovery skipped:[/yellow] {exc}")
        return None


def prompt_select_schemas(schemas: list[str]) -> list[str] | None:
    """Show a numbered list of schemas and prompt for a selection; None means 'all'."""
    table = Table(title="schemas")
    table.add_column("#", justify="right")
    table.add_column("schema")
    for i, name in enumerate(schemas, start=1):
        table.add_row(str(i), name)
    _console.print(table)
    while True:
        choice = typer.prompt("Select schemas (e.g. 1,3,5-7) or 'all'", default="all")
        if choice.strip().lower() == "all":
            return None
        try:
            indices = parse_index_ranges(choice, len(schemas))
        except ValueError as exc:
            _console.print(f"[red]{exc}[/red]")
            continue
        if not indices:
            _console.print("[red]select at least one schema, or 'all'[/red]")
            continue
        return [schemas[i - 1] for i in sorted(indices)]


def prompt_select_tables(relations: list[RelationSchema]) -> list[str] | None:
    """Show a numbered list of tables and prompt for a selection; None means 'all'."""
    if not relations or not typer.confirm("Narrow down to specific tables too?", default=False):
        return None
    names = [r.relation for r in relations]
    table = Table(title="tables")
    table.add_column("#", justify="right")
    table.add_column("table")
    for i, name in enumerate(names, start=1):
        table.add_row(str(i), name)
    _console.print(table)
    while True:
        choice = typer.prompt(
            "Select tables — indices/ranges (e.g. 1,3,5-7), glob patterns (e.g. fact_*), or 'all'",
            default="all",
        )
        if choice.strip().lower() == "all":
            return None
        try:
            selected = parse_table_tokens(choice, names)
        except ValueError as exc:
            _console.print(f"[red]{exc}[/red]")
            continue
        if not selected:
            _console.print("[red]select at least one table, or 'all'[/red]")
            continue
        return selected


def parse_index_ranges(text: str, count: int) -> set[int]:
    """Parse comma-separated 1-based indices/ranges (e.g. '1,3,5-7') into a validated set."""
    indices: set[int] = set()
    for raw_token in text.split(","):
        token = raw_token.strip()
        if not token:
            continue
        if "-" in token:
            start_s, _, end_s = token.partition("-")
            if not (start_s.isdigit() and end_s.isdigit()):
                raise ValueError(f"invalid range: {token!r}")
            start, end = int(start_s), int(end_s)
            if start > end:
                raise ValueError(f"invalid range: {token!r}")
            candidates: range | list[int] = range(start, end + 1)
        else:
            if not token.isdigit():
                raise ValueError(f"invalid index: {token!r}")
            candidates = [int(token)]
        for i in candidates:
            if not 1 <= i <= count:
                raise ValueError(f"index {i} out of range (1-{count})")
            indices.add(i)
    return indices


def is_index_range(token: str) -> bool:
    """True when token is a bare index ('7') or index range ('5-7'), not a glob pattern."""
    if "-" in token:
        start, _, end = token.partition("-")
        return start.isdigit() and end.isdigit()
    return token.isdigit()


def parse_table_tokens(text: str, names: list[str]) -> list[str]:
    """Parse comma-separated tokens: index/range tokens resolve to names; other
    tokens are kept verbatim as glob patterns (matched at introspection time)."""
    count = len(names)
    selected: list[str] = []
    seen: set[str] = set()
    for raw_token in text.split(","):
        token = raw_token.strip()
        if not token:
            continue
        if is_index_range(token):
            for i in sorted(parse_index_ranges(token, count)):
                name = names[i - 1]
                if name not in seen:
                    seen.add(name)
                    selected.append(name)
        elif token not in seen:
            seen.add(token)
            selected.append(token)
    return selected
