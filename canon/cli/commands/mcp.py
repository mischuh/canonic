"""``canon mcp`` — MCP daemon control (stubs; E8)."""

import typer

from canon.cli.commands import not_implemented

app = typer.Typer(name="mcp", help="Control the local MCP daemon.")


@app.command("start")
def start(ctx: typer.Context) -> None:
    """Start the local MCP daemon."""
    not_implemented(ctx, "mcp start")


@app.command("stop")
def stop(ctx: typer.Context) -> None:
    """Stop the local MCP daemon."""
    not_implemented(ctx, "mcp stop")


@app.command("status")
def status(ctx: typer.Context) -> None:
    """Report whether the MCP daemon is running."""
    not_implemented(ctx, "mcp status")
