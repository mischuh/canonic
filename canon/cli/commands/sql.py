"""``canon sql`` — read-only SQL escape hatch (stub; E2)."""

from typing import Annotated

import typer

from canon.cli._errors import handle_errors
from canon.cli.commands import not_implemented


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
    """Execute a read-only SQL string on a named connection (core.run_sql)."""
    not_implemented(ctx, "sql")
