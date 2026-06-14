"""``canon sl`` — semantic-layer resolve/compile (stubs; E5)."""

from pathlib import Path
from typing import Annotated

import typer

from canon.cli.commands import not_implemented

app = typer.Typer(name="sl", help="Semantic layer: resolve and compile.")


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
