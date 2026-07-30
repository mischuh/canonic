"""``canonic validate`` -- check contracts against semantic sources without executing anything."""

from __future__ import annotations

import json

import typer
from rich.console import Console

from canonic.cli._errors import get_cli_context, handle_errors
from canonic.config import find_project_root
from canonic.contracts.validate import validate_contracts

_console = Console(soft_wrap=True)


@handle_errors
def validate(ctx: typer.Context) -> None:
    """Validate every contract against the project's semantic sources.

    Read-only and connection-free: checks that every metric binding's
    ``canonical.source``/``measure``, every guardrail's ``applies_to``, every
    finality rule, and every assertion resolve against ``semantics/`` and
    ``contracts/``. These are the same cross-surface checks ``validate_contracts``
    already runs on the ingestion write path (``canonic ingest``) -- this makes
    them available as a standalone read-path check, so a broken contract is caught
    at write time instead of the next time a query happens to hit it.
    """
    json_output = get_cli_context(ctx).json_output
    root = find_project_root()
    if root is None:
        msg = "no canonic project found; run from inside a project directory"
        if json_output:
            typer.echo(json.dumps({"error": msg}))
        else:
            _console.print(f"[red]error:[/red] {msg}")
        raise typer.Exit(1)

    validate_contracts(root)

    if json_output:
        typer.echo(json.dumps({"status": "ok", "project_root": str(root)}))
    else:
        _console.print(f"[green]ok[/green]: contracts are valid for {root}")
