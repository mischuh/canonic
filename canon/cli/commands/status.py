"""``canon status`` — report project root, config version, and ``.canon/`` presence."""

import json

import typer
from rich.console import Console

from canon.cli._errors import get_cli_context
from canon.config import ConfigError, find_project_root, load_config

_console = Console(soft_wrap=True)


def status(ctx: typer.Context) -> None:
    """Show the current canon project root, config version, and local state presence."""
    json_output = get_cli_context(ctx).json_output
    root = find_project_root()

    if root is None:
        if json_output:
            typer.echo(json.dumps({"project_root": None}))
        else:
            _console.print("no canon project found")
        return

    config_version: int | None = None
    config_error: str | None = None
    try:
        config_version = load_config(root / "canon.yaml").version
    except ConfigError as exc:
        config_error = str(exc)

    dotcanon_present = (root / ".canon").is_dir()

    if json_output:
        typer.echo(
            json.dumps(
                {
                    "project_root": str(root),
                    "config_version": config_version,
                    "config_error": config_error,
                    "dotcanon_present": dotcanon_present,
                }
            )
        )
        return

    _console.print(f"project root:   [bold]{root}[/bold]")
    if config_error is not None:
        _console.print(f"config version: [red]invalid[/red] ({config_error})")
    else:
        _console.print(f"config version: {config_version}")
    _console.print(f".canon/:        {'present' if dotcanon_present else 'absent'}")
