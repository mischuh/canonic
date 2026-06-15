"""CLI subcommand groups. Each module exposes a ``typer.Typer()`` named ``app``."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import typer
from rich.console import Console

from canon.cli._errors import get_cli_context

if TYPE_CHECKING:
    from canon.core.service import CanonService

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


def load_service(ctx: typer.Context) -> CanonService:
    """Locate the enclosing canon project and build its :class:`CanonService`.

    Capability commands (``query``, ``sql``) share this so project discovery and
    service wiring live in one place. Exits 1 with a clear message — not a
    traceback — when run outside a project.
    """
    from canon.config import find_project_root
    from canon.core.service import CanonService

    root = find_project_root()
    if root is None:
        msg = "no canon project found — run from inside a project directory"
        if get_cli_context(ctx).json_output:
            typer.echo(json.dumps({"error": msg}))
        else:
            _console.print(f"[red]error:[/red] {msg}")
        raise typer.Exit(1)
    return CanonService.from_project(root)
