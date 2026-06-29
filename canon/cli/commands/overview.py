"""``canon overview`` — grouped discovery entry point (E7 §3, S12)."""

from __future__ import annotations

import json
from typing import Annotated

import typer
from rich.console import Console

from canon.cli._errors import get_cli_context, handle_errors
from canon.cli.commands import load_service

_console = Console()


@handle_errors
def overview(
    ctx: typer.Context,
    domain: Annotated[
        str | None,
        typer.Option("--domain", help="Filter to a single domain (owning-source name)."),
    ] = None,
) -> None:
    """Show active metrics grouped by domain with sample questions (core.get_overview).

    This is the recommended first command for a new user or agent — it provides
    a scannable map of what is askable before drilling into list_metrics / query.
    """
    service = load_service(ctx)
    result = service.get_overview(domain=domain)
    payload = result.model_dump(mode="json")

    if get_cli_context(ctx).json_output:
        typer.echo(json.dumps(payload))
        return

    if not result.domains:
        _console.print("[yellow]no domains found[/yellow]")
        return

    for group in result.domains:
        _console.print(
            f"\n[bold]{group.name}[/bold]  ({', '.join(m.label for m in group.metrics)})"
        )
        for q in group.sample_questions:
            _console.print(f"  • {q}")
