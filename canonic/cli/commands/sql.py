"""``canonic sql`` — read-only SQL escape hatch (E2)."""

from __future__ import annotations

import asyncio
import json
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from canonic.cli._errors import get_cli_context, handle_errors
from canonic.cli.commands import load_service

_console = Console()


@handle_errors
def sql(
    ctx: typer.Context,
    statement: Annotated[str, typer.Argument(help="A read-only SELECT statement.")],
    connection: Annotated[
        str | None,
        typer.Option(
            "--connection", "-c", help="Connection id (defaults to project.default_connection)."
        ),
    ] = None,
) -> None:
    """Execute a read-only SQL string on a named connection.

    Non-SELECT statements are rejected with ``READ_ONLY_VIOLATION`` (exit 11).
    """
    service = load_service(ctx)
    result = asyncio.run(service.run_sql(statement, connection=connection))

    payload = result.model_dump(mode="json")
    if get_cli_context(ctx).json_output:
        typer.echo(json.dumps(payload))
        return

    table = Table(show_header=True, header_style="bold")
    for col in result.columns:
        table.add_column(col.name)
    for row in result.rows:
        table.add_row(*(str(v) for v in row))
    _console.print(table)
    if result.truncated:
        _console.print("[yellow]note:[/yellow] result truncated at the connection row limit")
