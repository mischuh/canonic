"""``canon sl`` — semantic-layer resolve/compile/describe (E7 §3)."""

from __future__ import annotations

import json
from pathlib import Path  # noqa: TC003 — runtime type for typer Option
from typing import Annotated

import typer

from canon.cli._errors import get_cli_context, handle_errors
from canon.cli.commands import load_service, not_implemented

app = typer.Typer(name="sl", help="Semantic layer: resolve, compile, and describe.")


@app.command("resolve")
def resolve(
    ctx: typer.Context,
    name: Annotated[str, typer.Argument(help="Metric/dimension name.")],
) -> None:
    """Resolve a name to its canonical binding (core.resolve)."""
    not_implemented(ctx, "sl resolve")


@app.command("compile")
def compile_(
    ctx: typer.Context,
    file: Annotated[Path, typer.Option("-f", "--file", help="Semantic query JSON file.")],
) -> None:
    """Compile a semantic query to SQL + metadata without executing (core.compile)."""
    not_implemented(ctx, "sl compile")


@handle_errors
@app.command("describe")
def describe(
    ctx: typer.Context,
    name: Annotated[str, typer.Argument(help="Metric name or alias.")],
) -> None:
    """Return grain, dimensions, measures, and freshness for one metric (core.describe_metric).

    With ``--json`` the output matches the MCP ``describe_metric`` tool payload byte-for-byte.
    """
    from rich.console import Console

    service = load_service(ctx)
    detail = service.describe_metric(name)
    payload = detail.model_dump(mode="json")

    if get_cli_context(ctx).json_output:
        typer.echo(json.dumps(payload))
        return

    console = Console()
    console.print(f"[bold]{detail.metric}[/bold]  source={detail.source or '(composite)'}")
    if detail.grain:
        console.print(f"  grain:      {', '.join(detail.grain)}")
    if detail.dimensions:
        console.print(f"  dimensions: {', '.join(d.name for d in detail.dimensions)}")
    if detail.freshness:
        console.print(f"  freshness:  {detail.freshness.last_validated_at}")
    if detail.examples:
        console.print("  examples:")
        for ex in detail.examples:
            dims = ", ".join(ex.query.dimensions) if ex.query.dimensions else "(none)"
            console.print(f"    - dims=[{dims}]  origin={ex.origin}")
