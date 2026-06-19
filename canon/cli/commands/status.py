"""``canon status`` — report project root, config version, and ``.canon/`` presence."""

import json

import typer
from rich.console import Console

from canon.cli._errors import get_cli_context
from canon.config import ConfigError, find_project_root, load_config
from canon.contract import CONTRACT_SCHEMA
from canon.instrumentation.report import build_report, read_events

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

    events = read_events(root, kind="served_answer")
    rep = build_report(events)
    error_count = rep.count - rep.error_distribution.get("ok", 0)
    latency_p95: int | None = rep.latency.p95_ms if rep.latency is not None else None

    if json_output:
        typer.echo(
            json.dumps(
                {
                    "project_root": str(root),
                    "config_version": config_version,
                    "config_error": config_error,
                    "dotcanon_present": dotcanon_present,
                    "contract_schema": CONTRACT_SCHEMA,
                    "events": {
                        "count": rep.count,
                        "error_count": error_count,
                        "latency_p95_ms": latency_p95,
                    },
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
    _console.print(f"contract:       {CONTRACT_SCHEMA}")
    if rep.count > 0:
        p95_str = f"  p95 {latency_p95}ms" if latency_p95 is not None else ""
        _console.print(f"events:         {rep.count} served · errors {error_count}{p95_str}")
