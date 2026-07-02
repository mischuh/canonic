"""``canonic completion`` — shell completion (stub)."""

import typer

from canonic.cli.commands import not_implemented


def completion(ctx: typer.Context) -> None:
    """Generate shell completion script."""
    not_implemented(ctx, "completion")
