"""``canonic query`` — compile + execute a semantic query read-only (E5 → E2)."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path  # noqa: TC003 — runtime type for the typer Option
from typing import TYPE_CHECKING, Annotated

import typer
from rich.console import Console
from rich.table import Table

from canonic.cli._errors import get_cli_context, handle_errors
from canonic.cli.commands import build_semantic_query, load_service

if TYPE_CHECKING:
    from canonic.core.models import QueryResult

_console = Console()


@handle_errors
def query(
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
    harness: Annotated[
        bool,
        typer.Option(
            "--harness",
            help="Benchmark/CI mode: run matching assertions and exit 10 on a mismatch.",
        ),
    ] = False,
) -> None:
    """Resolve, compile, and execute a semantic query read-only.

    Either ``-f``/``--file`` (a JSON file with the
    :class:`~canonic.compiler.SemanticQuery` shape:
    ``{"metrics": [...], "dimensions": [...], "filters": [...], "limit": null}``) or
    the inline ``--metrics``/``--dimensions``/``--filter`` flags — never both.

    With ``--harness`` (benchmark/CI mode), every assertion matching the query is executed
    and any divergence from its expected result exits 10 (``ASSERTION_FAILED``); without it,
    assertions are informational and never block.
    """
    sq = build_semantic_query(file, metrics, dimensions, filter_)
    service = load_service(ctx)
    result = asyncio.run(service.query(sq, harness=harness))

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
