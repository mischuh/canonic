"""Root Typer application for the ``canon`` CLI (E7 serving surface).

This adapter does transport translation only (SPEC §2.1): it parses options,
selects output format (``--json``), and dispatches to capability commands. All
real logic lives behind the core in later epics.
"""

from importlib.metadata import PackageNotFoundError, version
from typing import Annotated

import typer

from canon.cli._errors import get_cli_context
from canon.cli.commands import (
    completion,
    connection,
    evaluate,
    ingest,
    knowledge,
    mcp,
    query,
    report,
    setup,
    sl,
    sql,
    status,
)

app = typer.Typer(
    name="canon",
    help="The Open Context Layer for Data Agents.",
    add_completion=False,
    no_args_is_help=False,
)


def _version_callback(value: bool) -> None:
    if not value:
        return
    try:
        typer.echo(version("canon"))
    except PackageNotFoundError:  # pragma: no cover — only when running uninstalled
        typer.echo("unknown")
    raise typer.Exit(0)


@app.callback(invoke_without_command=True)
def main(
    ctx: typer.Context,
    json_output: Annotated[
        bool,
        typer.Option("--json", "-j", help="Emit raw structured JSON instead of formatted text."),
    ] = False,
    _version: Annotated[
        bool,
        typer.Option(
            "--version",
            help="Show the canon version and exit.",
            callback=_version_callback,
            is_eager=True,
        ),
    ] = False,
) -> None:
    """Canon CLI entry point: sets global flags and dispatches subcommands."""
    get_cli_context(ctx).json_output = json_output
    if ctx.invoked_subcommand is None:
        # Bare ``canon`` will open the interactive wizard (E1); not implemented yet.
        typer.echo("interactive mode not implemented yet — run `canon --help`.")
        raise typer.Exit(0)


# Command groups (multi-command).
app.add_typer(connection.app, name="connection")
app.add_typer(sl.app, name="sl")
app.add_typer(mcp.app, name="mcp")
app.add_typer(knowledge.app, name="knowledge")
app.add_typer(evaluate.app, name="eval")

# Top-level single commands.
app.command("setup")(setup.setup)
app.command("ingest")(ingest.ingest)
app.command("query")(query.query)
app.command("sql")(sql.sql)
app.command("status")(status.status)
app.command("report")(report.report)
app.command("completion")(completion.completion)
