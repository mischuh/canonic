"""``canonic knowledge`` — knowledge search (stub; E6, P1)."""

import typer

from canonic.cli.commands import not_implemented

app = typer.Typer(name="knowledge", help="Search project knowledge and semantics.")


@app.command("search")
def search(ctx: typer.Context, query: str = typer.Argument(..., help="Search text.")) -> None:
    """Hybrid search over knowledge + semantics (core.search)."""
    not_implemented(ctx, "knowledge search")
