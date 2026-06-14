"""``canon connection`` — E2 connection lifecycle (stubs)."""

import typer

from canon.cli.commands import not_implemented

app = typer.Typer(name="connection", help="Manage source connections.")


@app.command("add")
def add(ctx: typer.Context) -> None:
    """Add a new source connection."""
    not_implemented(ctx, "connection add")


@app.command("test")
def test(ctx: typer.Context) -> None:
    """Test connectivity and read-only access for a connection."""
    not_implemented(ctx, "connection test")


@app.command("list")
def list_(ctx: typer.Context) -> None:
    """List configured connections."""
    not_implemented(ctx, "connection list")


@app.command("remove")
def remove(ctx: typer.Context) -> None:
    """Remove a configured connection."""
    not_implemented(ctx, "connection remove")
