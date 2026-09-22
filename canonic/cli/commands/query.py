"""``canonic query`` — compile + execute a semantic query read-only (E5 → E2), plus manage
personal saved queries (AMENDMENT-user-scoped-queries-reports, S19-S21).

The bare form (``canonic query --metrics ...``) is unchanged; ``save``/``list``/``delete`` are
new subcommands, following the same group-with-a-default-action shape ``canonic report`` already
uses.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path  # noqa: TC003 — runtime type for the typer Option
from typing import TYPE_CHECKING, Annotated

import typer
from rich.console import Console
from rich.table import Table

from canonic.cli._errors import get_cli_context, handle_errors
from canonic.cli._tenant import TenantOption, cli_tenant_principal
from canonic.cli.commands import build_semantic_query, load_service

if TYPE_CHECKING:
    from canonic.core.models import QueryResult, SavedQuerySummary

_console = Console()

app = typer.Typer(name="query", help="Compile/execute a semantic query; manage saved queries.")


@app.callback(invoke_without_command=True)
def _query_group(
    ctx: typer.Context,
    file: Annotated[
        Path | None,
        typer.Option("-f", "--file", help="Semantic query JSON file.", exists=True, readable=True),
    ] = None,
    metrics: Annotated[
        list[str] | None,
        typer.Option("--metrics", help="Metric name(s), comma-separated and/or repeatable."),
    ] = None,
    dimensions: Annotated[
        list[str] | None,
        typer.Option("--dimensions", help="Dimension name(s), comma-separated and/or repeatable."),
    ] = None,
    filter_: Annotated[
        list[str] | None,
        typer.Option("--filter", help="Filter as field=value or field:op:value (repeatable)."),
    ] = None,
    via: Annotated[
        list[str] | None,
        typer.Option(
            "--via",
            help="Join-path alias prefix (comma-separated and/or repeatable) to disambiguate "
            "an ambiguous_join_path error.",
        ),
    ] = None,
    limit: Annotated[
        int | None,
        typer.Option("--limit", help="Row cap injected by the dialect adapter."),
    ] = None,
    harness: Annotated[
        bool,
        typer.Option(
            "--harness",
            help="Benchmark/CI mode: run matching assertions and exit 10 on a mismatch.",
        ),
    ] = False,
    tenant: TenantOption = None,
) -> None:
    """Resolve, compile, and execute a semantic query read-only.

    Either ``-f``/``--file`` (a JSON file with the
    :class:`~canonic.compiler.SemanticQuery` shape:
    ``{"metrics": [...], "dimensions": [...], "filters": [...], "via": [...], "limit": null}``)
    or the inline ``--metrics``/``--dimensions``/``--filter``/``--via``/``--limit`` flags —
    never both.

    With ``--harness`` (benchmark/CI mode), every assertion matching the query is executed
    and any divergence from its expected result exits 10 (``ASSERTION_FAILED``); without it,
    assertions are informational and never block.

    See ``canonic query save``/``list``/``delete`` to manage personal saved queries.
    """
    if ctx.invoked_subcommand is not None:
        return
    # handle_errors detects `ctx` via kwargs (matching how Click invokes a command's own
    # callback); called positionally, its isinstance(..., typer.Context) fallback never
    # matches the real runtime click.Context object, so json_output silently defaults to
    # False. Pass by keyword to hit the primary lookup instead.
    _run_query(
        ctx=ctx,
        file=file,
        metrics=metrics,
        dimensions=dimensions,
        filter_=filter_,
        via=via,
        limit=limit,
        harness=harness,
        tenant=tenant,
    )


@handle_errors
def _run_query(
    ctx: typer.Context,
    file: Path | None,
    metrics: list[str] | None,
    dimensions: list[str] | None,
    filter_: list[str] | None,
    via: list[str] | None,
    limit: int | None,
    harness: bool,
    tenant: str | None,
) -> None:
    # SPEC-E12 §7: --tenant is local-development/platform-operator only, always warns.
    principal = cli_tenant_principal(tenant)
    sq = build_semantic_query(file, metrics, dimensions, filter_, via, limit)
    service = load_service(ctx)
    result = asyncio.run(service.query(sq, harness=harness, principal=principal))

    # ``mode="json"`` yields JSON-native primitives (Decimal/datetime → str/number)
    # so this payload is byte-identical to the MCP ``query`` tool's serialized result.
    payload = result.model_dump(mode="json")
    if get_cli_context(ctx).json_output:
        typer.echo(json.dumps(payload))
        return

    _render(result)


def _render(result: QueryResult) -> None:
    """Render a QueryResult as a Rich table for human (non-JSON) output."""
    rs = result.result
    table = Table(show_header=True, header_style="bold")
    for col in rs.columns:
        table.add_column(col.name)
    for row in rs.rows:
        table.add_row(*(str(v) for v in row))
    _console.print(table)
    if rs.truncated:
        _console.print("[yellow]note:[/yellow] result truncated at the connection row limit")


@app.command("save")
@handle_errors
def save(
    ctx: typer.Context,
    file: Annotated[
        Path | None,
        typer.Option("-f", "--file", help="Semantic query JSON file.", exists=True, readable=True),
    ] = None,
    metrics: Annotated[
        list[str] | None,
        typer.Option("--metrics", help="Metric name(s), comma-separated and/or repeatable."),
    ] = None,
    dimensions: Annotated[
        list[str] | None,
        typer.Option("--dimensions", help="Dimension name(s), comma-separated and/or repeatable."),
    ] = None,
    filter_: Annotated[
        list[str] | None,
        typer.Option("--filter", help="Filter as field=value or field:op:value (repeatable)."),
    ] = None,
    via: Annotated[list[str] | None, typer.Option("--via", help="Join-path alias prefix.")] = None,
    limit: Annotated[int | None, typer.Option("--limit", help="Row cap.")] = None,
    title: Annotated[str | None, typer.Option("--title", help="Human-readable title.")] = None,
    question: Annotated[
        str | None,
        typer.Option("--question", help="Plain-language phrasing (feeds get_overview)."),
    ] = None,
    user: Annotated[
        str | None, typer.Option("--user", help="Owning user id (required to save).")
    ] = None,
    tenant: TenantOption = None,
) -> None:
    """Save the current query for later reuse under your own scope (core.save_query, S19).

    Validates the query compiles before saving. No PR/merge review required — everything
    under ``reports/user/<own-id>/queries/`` is self-service, but every write is still an
    attributed git commit. Refused with ``TENANT_FORBIDDEN`` when the caller's role denies
    ``manage_saved_content``.
    """
    # SPEC-E12 §7: --tenant is local-development/platform-operator only, always warns.
    principal = cli_tenant_principal(tenant)
    sq = build_semantic_query(file, metrics, dimensions, filter_, via, limit)
    service = load_service(ctx)
    summary = asyncio.run(
        service.save_query(sq, title=title, question=question, user=user, principal=principal)
    )
    payload = summary.model_dump(mode="json")
    if get_cli_context(ctx).json_output:
        typer.echo(json.dumps(payload))
        return
    _console.print(f"[green]saved[/green] {summary.id}")


@app.command("list")
@handle_errors
def list_(
    ctx: typer.Context,
    user: Annotated[
        str | None, typer.Option("--user", help="Owning user id (required to list).")
    ] = None,
    tenant: TenantOption = None,
) -> None:
    """List your own saved queries — never another user's (core.list_saved_queries, S20)."""
    principal = cli_tenant_principal(tenant)
    service = load_service(ctx)
    summaries = service.list_saved_queries(user=user, principal=principal)
    payload = {"queries": [s.model_dump(mode="json") for s in summaries]}

    if get_cli_context(ctx).json_output:
        typer.echo(json.dumps(payload))
        return

    if not summaries:
        _console.print("[yellow]no saved queries found[/yellow]")
        return
    _render_saved_queries(summaries)


def _render_saved_queries(summaries: list[SavedQuerySummary]) -> None:
    table = Table(show_header=True, header_style="bold")
    table.add_column("id")
    table.add_column("title")
    table.add_column("metrics")
    table.add_column("dimensions")
    for s in summaries:
        table.add_row(s.id, s.title, ", ".join(s.metrics), ", ".join(s.dimensions))
    _console.print(table)


@app.command("delete")
@handle_errors
def delete(
    ctx: typer.Context,
    query_id: Annotated[str, typer.Argument(help="Saved query id to delete.")],
    user: Annotated[
        str | None, typer.Option("--user", help="Owning user id (required to delete).")
    ] = None,
    tenant: TenantOption = None,
) -> None:
    """Remove one of your own saved queries; never a report (core.delete_query, S21, S23)."""
    principal = cli_tenant_principal(tenant)
    service = load_service(ctx)
    asyncio.run(service.delete_query(query_id, user=user, principal=principal))
    if get_cli_context(ctx).json_output:
        typer.echo(json.dumps({"deleted": query_id}))
        return
    _console.print(f"[green]deleted[/green] {query_id}")
