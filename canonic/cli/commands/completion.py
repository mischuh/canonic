"""``canonic completion`` — print a shell completion script (bash/zsh/fish)."""

from __future__ import annotations

from typing import Annotated

import typer
from click.shell_completion import get_completion_class
from rich.console import Console

_console = Console()


def _detect_shell() -> str | None:
    """Best-effort detection of the invoking shell; None if it can't be determined."""
    import shellingham  # type: ignore[import-untyped]

    try:
        name, _path = shellingham.detect_shell()
    except shellingham.ShellDetectionFailure:
        return None
    return str(name)


def completion(
    ctx: typer.Context,
    shell: Annotated[
        str | None,
        typer.Option("--shell", help="bash, zsh, or fish (auto-detected if omitted)."),
    ] = None,
) -> None:
    """Print a shell completion script for ``canonic`` to stdout.

    Wire it into your shell, e.g.::

        eval "$(canonic completion --shell zsh)"              # add this line to ~/.zshrc
        canonic completion --shell bash > ~/.bash_completion.d/canonic

    Powered by Click's built-in completion machinery; the same script works for the
    ``can`` alias since both entry points share this command tree.
    """
    resolved_shell = shell or _detect_shell()
    if resolved_shell is None:
        _console.print(
            "[red]error:[/red] could not detect your shell; pass --shell explicitly "
            "(bash, zsh, or fish)"
        )
        raise typer.Exit(1)

    completion_cls = get_completion_class(resolved_shell)
    if completion_cls is None:
        _console.print(f"[red]error:[/red] shell {resolved_shell!r} is not supported")
        raise typer.Exit(1)

    root_ctx = ctx.find_root()
    prog_name = root_ctx.info_name or "canonic"
    complete_var = f"_{prog_name.upper().replace('-', '_')}_COMPLETE"
    comp = completion_cls(root_ctx.command, {}, prog_name, complete_var)  # type: ignore[arg-type]
    typer.echo(comp.source())
