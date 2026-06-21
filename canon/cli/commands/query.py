"""``canon query`` — compile + execute a semantic query read-only (E5 → E2)."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path  # noqa: TC003 — runtime type for the typer Option
from typing import TYPE_CHECKING, Annotated

import typer
from rich.console import Console
from rich.table import Table

from canon.cli._errors import get_cli_context, handle_errors
from canon.cli.commands import load_service
from canon.compiler import SemanticQuery

if TYPE_CHECKING:
    from canon.core.models import QueryResult

_console = Console()


@handle_errors
def query(
    ctx: typer.Context,
    file: Annotated[
        Path,
        typer.Option("-f", "--file", help="Semantic query JSON file.", exists=True, readable=True),
    ],
    harness: Annotated[
        bool,
        typer.Option(
            "--harness",
            help="Benchmark/CI mode: run matching assertions and exit 10 on a mismatch.",
        ),
    ] = False,
) -> None:
    """Resolve, compile, and execute a semantic query read-only (core.query).

    The query file is JSON with the :class:`~canon.compiler.SemanticQuery` shape:
    ``{"metrics": [...], "dimensions": [...], "filters": [...], "limit": null}``.

    With ``--harness`` (benchmark/CI mode, SPEC-Fuller-E15 §3.2), every assertion matching
    the query is executed and any divergence from its expected result exits 10
    (``ASSERTION_FAILED``); without it, assertions are informational and never block.
    """
    sq = SemanticQuery.model_validate_json(file.read_text())
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
