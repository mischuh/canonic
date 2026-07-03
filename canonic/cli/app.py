"""Root Typer application for the ``canonic`` CLI (E7 serving surface).

This adapter does transport translation only (SPEC §2.1): it parses options,
selects output format (``--json``), and dispatches to capability commands. All
real logic lives behind the core in later epics.
"""

from importlib.metadata import PackageNotFoundError, version
from typing import Annotated

import typer

from canonic.cli._errors import get_cli_context
from canonic.cli.commands import (
    apply,
    assertions,
    completion,
    connection,
    evaluate,
    ingest,
    knowledge,
    mcp,
    overview,
    query,
    report,
    review,
    setup,
    sl,
    sql,
    status,
)

app = typer.Typer(
    name="canonic",
    help="The Open Context Layer for Data Agents.",
    add_completion=False,
    no_args_is_help=False,
)


def _version_callback(value: bool) -> None:
    if not value:
        return
    try:
        typer.echo(version("canonic"))
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
            help="Show the canonic version and exit.",
            callback=_version_callback,
            is_eager=True,
        ),
    ] = False,
) -> None:
    """Canonic CLI entry point: sets global flags and dispatches subcommands."""
    get_cli_context(ctx).json_output = json_output

    from canonic.log import _effective_log_params, configure_logging

    level, file, format = _effective_log_params("WARNING", None)
    configure_logging(level=level, file=file, format=format)

    if ctx.invoked_subcommand is None:
        if json_output:
            typer.echo("interactive mode is not available with --json — run `canonic --help`.")
            raise typer.Exit(2)
        from canonic.cli.commands.setup import run_interactive

        run_interactive()
        raise typer.Exit(0)


# Command groups (multi-command).
app.add_typer(connection.app, name="connection")
app.add_typer(sl.app, name="sl")
app.add_typer(mcp.app, name="mcp")
app.add_typer(knowledge.app, name="knowledge")
app.add_typer(evaluate.app, name="eval")

# Top-level single commands.
app.command("overview")(overview.overview)
app.command("setup")(setup.setup)
app.command("ingest")(ingest.ingest)
app.command("review")(review.review)
app.command("apply")(apply.apply)
app.command("query")(query.query)
app.command("assert")(assertions.assert_)
app.command("sql")(sql.sql)
app.command("status")(status.status)
app.command("report")(report.report)
app.command("completion")(completion.completion)
