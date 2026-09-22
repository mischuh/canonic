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
from canonic.cli._tenant import TenantOption, cli_tenant_principal
from canonic.cli.commands import load_service
from canonic.compiler.query import parse_filter_flag

if TYPE_CHECKING:
    from canonic.core.models import ReportRunResult, ReportSummary

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
    user: Annotated[
        str | None,
        typer.Option("--user", help="Requesting user id, to see your own personal reports too."),
    ] = None,
    mine: Annotated[bool, typer.Option("--mine", help="Only your own personal reports.")] = False,
    global_: Annotated[
        bool, typer.Option("--global", help="Only data-team curated (global) reports.")
    ] = False,
    all_: Annotated[bool, typer.Option("--all", help="Both scopes (the default).")] = False,
) -> None:
    """List reports visible to you: ``global/`` + your own scope (core.list_reports).

    Default is both scopes (``--mine``/``--global``/``--all`` are mutually exclusive).
    Never includes saved queries — see ``canonic query list`` for those. With ``--json`` the
    output matches the MCP ``list_reports`` tool payload byte-for-byte.
    """
    if sum([mine, global_, all_]) > 1:
        raise typer.BadParameter("--mine, --global, and --all are mutually exclusive")

    service = load_service(ctx)
    summaries = service.list_reports(domain=domain, user=user)
    if mine:
        summaries = [s for s in summaries if s.scope != "global"]
    elif global_:
        summaries = [s for s in summaries if s.scope == "global"]

    payload = {"reports": [s.model_dump(mode="json") for s in summaries]}

    if get_cli_context(ctx).json_output:
        typer.echo(json.dumps(payload))
        return

    if not summaries:
        _console.print("[yellow]no reports found[/yellow]")
        return

    _render_list(summaries)


def _render_list(summaries: list[ReportSummary]) -> None:
    table = Table(show_header=True, header_style="bold")
    table.add_column("id")
    table.add_column("title")
    table.add_column("description")
    table.add_column("owner")
    table.add_column("domain")
    table.add_column("scope")
    for s in summaries:
        table.add_row(s.id, s.title, s.description or "", s.owner or "", s.domain or "", s.scope)
    _console.print(table)


@app.command("describe")
@handle_errors
def describe(
    ctx: typer.Context,
    report_id: Annotated[str, typer.Argument(help="Report or saved-query id to describe.")],
    user: Annotated[
        str | None, typer.Option("--user", help="Requesting user id, to see your own reports.")
    ] = None,
) -> None:
    """Show a report's sections (title, section id, metrics, dimensions) without running it.

    The read-only counterpart to ``run``: use this to find a section's stable id for
    ``canonic report update --remove-section``. With ``--json`` the output matches the MCP
    ``describe_report`` tool payload byte-for-byte.
    """
    service = load_service(ctx)
    structure = service.describe_report(report_id, user=user)
    payload = structure.model_dump(mode="json")
    if get_cli_context(ctx).json_output:
        typer.echo(json.dumps(payload))
        return

    table = Table(show_header=True, header_style="bold")
    table.add_column("id")
    table.add_column("title")
    table.add_column("metrics")
    table.add_column("dimensions")
    for section in structure.sections:
        table.add_row(
            section.id or "",
            section.title,
            ", ".join(section.metrics),
            ", ".join(section.dimensions),
        )
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
    filter_: Annotated[
        list[str] | None,
        typer.Option(
            "--filter",
            help="Filter as field=value or field:op:value (repeatable). Applied additively "
            "to every section's own filters — e.g. scope a whole report run to one "
            "merchant with --filter merchant_id=123.",
        ),
    ] = None,
    user: Annotated[
        str | None,
        typer.Option("--user", help="Requesting user id, for narrative access control."),
    ] = None,
    tenant: TenantOption = None,
) -> None:
    """Run every section of a committed report through query(), in declared order.

    A failing section does not abort the run: it appears with a structured
    ``{code, message}`` error in place of a result, and the other sections still return
    normally — the call as a whole exits 0. With ``--json`` the output matches the MCP
    ``run_report`` tool payload byte-for-byte.
    """
    # SPEC-E12 §7: --tenant is local-development/platform-operator only, always warns.
    principal = cli_tenant_principal(tenant)
    try:
        parsed_filters = [parse_filter_flag(f) for f in filter_ or []]
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc

    service = load_service(ctx)
    result = asyncio.run(
        service.run_report(
            report_id, as_of=as_of, filters=parsed_filters, user=user, principal=principal
        )
    )

    payload = result.model_dump(mode="json")
    if get_cli_context(ctx).json_output:
        typer.echo(json.dumps(payload))
        return

    _render(result)


@app.command("compose")
@handle_errors
def compose(
    ctx: typer.Context,
    title: Annotated[str, typer.Option("--title", help="Title for the new personal report.")],
    from_: Annotated[
        list[str],
        typer.Option("--from", help="Saved query id to include (repeatable, in order)."),
    ],
    user: Annotated[
        str | None, typer.Option("--user", help="Owning user id (required to compose).")
    ] = None,
    tenant: TenantOption = None,
) -> None:
    """Assemble your own saved queries into a new personal report (core.compose_report, S22).

    Each source query's definition is copied in, frozen at compose time — later editing or
    deleting a source query never changes this report. Refused with ``TENANT_FORBIDDEN`` when
    the caller's role denies ``manage_saved_content``.
    """
    principal = cli_tenant_principal(tenant)
    service = load_service(ctx)
    summary = asyncio.run(service.compose_report(title, from_, user=user, principal=principal))
    payload = summary.model_dump(mode="json")
    if get_cli_context(ctx).json_output:
        typer.echo(json.dumps(payload))
        return
    _console.print(f"[green]composed[/green] {summary.id}")


@app.command("update")
@handle_errors
def update(
    ctx: typer.Context,
    report_id: Annotated[str, typer.Argument(help="Your own personal report id to update.")],
    add_from: Annotated[
        list[str] | None,
        typer.Option("--add-from", help="Saved query id to append (repeatable)."),
    ] = None,
    remove_section: Annotated[
        list[str] | None,
        typer.Option("--remove-section", help="Section id to remove (repeatable)."),
    ] = None,
    user: Annotated[
        str | None, typer.Option("--user", help="Owning user id (required to update).")
    ] = None,
    tenant: TenantOption = None,
) -> None:
    """Add and/or remove sections on one of your own personal reports (core.update_report, S25).

    The report's id and existing sections are otherwise unchanged; a newly added section's
    query definition is copied at update time, same freeze semantics as ``compose``.
    """
    principal = cli_tenant_principal(tenant)
    service = load_service(ctx)
    summary = asyncio.run(
        service.update_report(
            report_id,
            add_from=add_from,
            remove_sections=remove_section,
            user=user,
            principal=principal,
        )
    )
    payload = summary.model_dump(mode="json")
    if get_cli_context(ctx).json_output:
        typer.echo(json.dumps(payload))
        return
    _console.print(f"[green]updated[/green] {summary.id}")


@app.command("delete")
@handle_errors
def delete(
    ctx: typer.Context,
    report_id: Annotated[str, typer.Argument(help="Your own personal report id to delete.")],
    user: Annotated[
        str | None, typer.Option("--user", help="Owning user id (required to delete).")
    ] = None,
    tenant: TenantOption = None,
) -> None:
    """Remove one of your own personal reports (core.delete_report, S23).

    Never removes the saved queries it was composed from, and is refused for a ``global/``
    report — removing one of those happens only through the standard PR-review workflow.
    """
    principal = cli_tenant_principal(tenant)
    service = load_service(ctx)
    asyncio.run(service.delete_report(report_id, user=user, principal=principal))
    if get_cli_context(ctx).json_output:
        typer.echo(json.dumps({"deleted": report_id}))
        return
    _console.print(f"[green]deleted[/green] {report_id}")


def _render(result: ReportRunResult) -> None:
    """Render a ReportRunResult as one Rich table per section for human (non-JSON) output."""
    for section in result.sections:
        header = section.title
        if section.id is not None:
            header += f" [dim]({section.id})[/dim]"
        _console.print(f"\n[bold]{header}[/bold]")
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
