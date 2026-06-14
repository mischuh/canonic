"""CLI subcommand groups. Each module exposes a ``typer.Typer()`` named ``app``."""

import json

import typer
from rich.console import Console

from canon.cli._errors import get_cli_context

_console = Console()


def not_implemented(ctx: typer.Context, feature: str) -> None:
    """Print a uniform ``not implemented yet`` notice and exit 0 (no traceback).

    Stub for capability commands whose logic lands in later epics (E2/E5/E6/E8/E9).
    """
    json_output = get_cli_context(ctx).json_output
    if json_output:
        typer.echo(json.dumps({"status": "not_implemented", "feature": feature}))
    else:
        _console.print(f"[yellow]{feature}[/yellow]: not implemented yet")
    raise typer.Exit(0)
