"""``canonic report`` — list and run curated, committed reports (AMENDMENT-curated-reports).

This adapter does transport translation only (SPEC §2.1): all orchestration lives in
:class:`canonic.core.reports.ReportService`. ``canonic report`` used to be the diagnostics
command now named ``canonic audit`` (AMENDMENT-audit-command-rename) — the bare, subcommand-less
form is intercepted below so that meaning is never silently repurposed.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime  # noqa: TC003 — runtime type for the typer Option
from typing import TYPE_CHECKING, Annotated

import typer
from rich.console import Console
from rich.table import Table

from canonic.cli._errors import get_cli_context, handle_errors
from canonic.cli.commands import load_service

if TYPE_CHECKING:
    from canonic.core.models import ReportRunResult

_console = Console()

app = typer.Typer(name="report", help="List and run curated, committed reports.")


@app.callback(invoke_without_command=True)
def _report_group(ctx: typer.Context) -> None:
    """Bare ``canonic report`` no longer shows diagnostics — see ``canonic audit``.

    ``canonic report`` used to alias the event-log diagnostics command
    (AMENDMENT-audit-command-rename); once curated reports claim the name, that alias
    cannot coexist. A bare invocation gets a clear pointer instead of silently returning
    something else (see that amendment's "Sequencing" §, step 3).
    """
    if ctx.invoked_subcommand is not None:
        return
    _console.print(
        '[red]error:[/red] "canonic report" now runs curated reports — did you mean '
        '"canonic audit"? Use "canonic report list" or "canonic report run <report-id>".'
    )
    raise typer.Exit(2)


@app.command("list")
@handle_errors
def list_(
    ctx: typer.Context,
    domain: Annotated[
        str | None,
        typer.Option("--domain", help="Filter to reports declaring this domain."),
    ] = None,
) -> None:
    """List committed reports: id, title, description, owner, domain (core.list_reports).

    With ``--json`` the output matches the MCP ``list_reports`` tool payload byte-for-byte.
    """
    service = load_service(ctx)
    summaries = service.list_reports(domain=domain)
    payload = {"reports": [s.model_dump(mode="json") for s in summaries]}

    if get_cli_context(ctx).json_output:
        typer.echo(json.dumps(payload))
        return

    if not summaries:
        _console.print("[yellow]no reports found[/yellow]")
        return

    table = Table(show_header=True, header_style="bold")
    table.add_column("id")
    table.add_column("title")
    table.add_column("description")
    table.add_column("owner")
    table.add_column("domain")
    for s in summaries:
        table.add_row(s.id, s.title, s.description or "", s.owner or "", s.domain or "")
    _console.print(table)


@app.command("run")
@handle_errors
def run(
    ctx: typer.Context,
    report_id: Annotated[str, typer.Argument(help="Committed report id to run.")],
    as_of: Annotated[
        datetime | None,
        typer.Option("--as-of", help="ISO-8601 reference point for finality watermark evaluation."),
    ] = None,
    user: Annotated[
        str | None,
        typer.Option("--user", help="Requesting user id, for narrative access control."),
    ] = None,
) -> None:
    """Run every section of a committed report through query(), in declared order.

    A failing section does not abort the run: it appears with a structured
    ``{code, message}`` error in place of a result, and the other sections still return
    normally — the call as a whole exits 0. With ``--json`` the output matches the MCP
    ``run_report`` tool payload byte-for-byte.
    """
    service = load_service(ctx)
    result = asyncio.run(service.run_report(report_id, as_of=as_of, user=user))

    payload = result.model_dump(mode="json")
    if get_cli_context(ctx).json_output:
        typer.echo(json.dumps(payload))
        return

    _render(result)


def _render(result: ReportRunResult) -> None:
    """Render a ReportRunResult as one Rich table per section for human (non-JSON) output."""
    for section in result.sections:
        _console.print(f"\n[bold]{section.title}[/bold]")
        if section.error is not None:
            code = section.error.get("code", "error")
            message = section.error.get("message", "")
            _console.print(f"  [red]{code}[/red]: {message}")
            continue
        assert section.result is not None  # noqa: S101 — exactly one of result/error is set
        rs = section.result.result
        table = Table(show_header=True, header_style="bold")
        for col in rs.columns:
            table.add_column(col.name)
        for row in rs.rows:
            table.add_row(*(str(v) for v in row))
        _console.print(table)
        if section.narrative is not None:
            _console.print(f"  [dim]{section.narrative.body}[/dim]")
