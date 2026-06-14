"""``canon query`` — compile + execute a semantic query (stub; E5→E2)."""

from pathlib import Path
from typing import Annotated

import typer

from canon.cli._errors import handle_errors
from canon.cli.commands import not_implemented


@handle_errors
def query(
    ctx: typer.Context,
    file: Annotated[Path, typer.Option("-f", "--file", help="Semantic query JSON file.")],
) -> None:
    """Resolve, compile, and execute a semantic query read-only (core.query)."""
    not_implemented(ctx, "query")
