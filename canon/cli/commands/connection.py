"""``canon connection`` — connection lifecycle commands."""

from __future__ import annotations

import asyncio
import json
from typing import Annotated, Any

import typer
from rich.console import Console
from rich.table import Table
from ruamel.yaml import YAML

from canon.cli._errors import get_cli_context, handle_errors
from canon.config import ConfigError, find_project_root, load_config
from canon.connectors.factory import default_factory

_console = Console()

app = typer.Typer(name="connection", help="Manage source connections.")


def _load_raw(path: Any) -> Any:
    yaml = YAML()
    with open(path) as f:
        return yaml.load(f)


def _write_raw(path: Any, raw: Any) -> None:
    yaml = YAML()
    yaml.default_flow_style = False
    with open(path, "w") as f:
        yaml.dump(raw, f)


def _project_or_exit(ctx: typer.Context) -> Any:
    """Return project root path or exit 1 with a clear message."""
    root = find_project_root()
    if root is None:
        msg = "no canon project found — run from inside a project directory"
        json_output = get_cli_context(ctx).json_output
        if json_output:
            typer.echo(json.dumps({"error": msg}))
        else:
            _console.print(f"[red]error:[/red] {msg}")
        raise typer.Exit(1)
    return root


@app.command("list")
@handle_errors
def list_(ctx: typer.Context) -> None:
    """List configured connections."""
    json_output = get_cli_context(ctx).json_output
    root = _project_or_exit(ctx)
    try:
        config = load_config(root / "canon.yaml")
    except ConfigError as exc:
        msg = str(exc)
        if json_output:
            typer.echo(json.dumps({"error": msg}))
        else:
            _console.print(f"[red]error:[/red] {msg}")
        raise typer.Exit(1) from exc

    default_id = config.project.default_connection

    if json_output:
        rows = [
            {
                "id": c.id,
                "type": c.type,
                "params": c.params,
                "default": c.id == default_id,
            }
            for c in config.connections
        ]
        typer.echo(json.dumps({"connections": rows}))
        return

    if not config.connections:
        _console.print("no connections configured")
        return

    table = Table(show_header=True, header_style="bold")
    table.add_column("id")
    table.add_column("type")
    table.add_column("default")
    table.add_column("params")
    for conn in config.connections:
        is_default = conn.id == default_id
        params_str = ", ".join(f"{k}={v}" for k, v in conn.params.items())
        table.add_row(
            conn.id,
            conn.type,
            "[green]✓[/green]" if is_default else "",
            params_str,
        )
    _console.print(table)


@app.command("test")
@handle_errors
def test(
    ctx: typer.Context,
    connection: Annotated[
        str | None,
        typer.Option("--connection", "-c", help="Connection id to test (default: all)."),
    ] = None,
) -> None:
    """Test connectivity for one or all connections."""
    json_output = get_cli_context(ctx).json_output
    root = _project_or_exit(ctx)
    try:
        config = load_config(root / "canon.yaml")
    except ConfigError as exc:
        msg = str(exc)
        if json_output:
            typer.echo(json.dumps({"error": msg}))
        else:
            _console.print(f"[red]error:[/red] {msg}")
        raise typer.Exit(1) from exc

    targets = (
        [c for c in config.connections if c.id == connection]
        if connection is not None
        else list(config.connections)
    )
    if not targets:
        msg = (
            f"connection {connection!r} not found"
            if connection is not None
            else "no connections configured"
        )
        if json_output:
            typer.echo(json.dumps({"error": msg}))
        else:
            _console.print(f"[red]error:[/red] {msg}")
        raise typer.Exit(1)

    async def _run_tests() -> list[dict[str, Any]]:
        results = []
        for conn in targets:
            connector = default_factory.create(conn)
            try:
                health = await connector.test_connection()
            finally:
                await connector.aclose()
            results.append({"id": conn.id, "status": health.status, "message": health.message})
        return results

    results = asyncio.run(_run_tests())
    all_ok = all(r["status"] == "ok" for r in results)

    if json_output:
        typer.echo(json.dumps({"results": results}))
        raise typer.Exit(0 if all_ok else 1)

    for r in results:
        status_str = "[green]ok[/green]" if r["status"] == "ok" else "[red]error[/red]"
        msg_str = f"  {r['message']}" if r["message"] else ""
        _console.print(f"{r['id']}: {status_str}{msg_str}")

    if not all_ok:
        raise typer.Exit(1)


@app.command("add")
@handle_errors
def add(
    ctx: typer.Context,
    id_: Annotated[str, typer.Option("--id", help="Unique connection identifier.")],
    type_: Annotated[str, typer.Option("--type", help="Connector type (sqlite, postgres, …).")],
    param: Annotated[
        list[str] | None,
        typer.Option("--param", "-p", help="Connector param as KEY=VALUE (repeatable)."),
    ] = None,
    credentials_ref: Annotated[
        str | None,
        typer.Option(
            "--credentials-ref",
            help="Credential reference (env:VAR, keyring:service, file:path).",
        ),
    ] = None,
    set_default: Annotated[
        bool,
        typer.Option("--set-default", help="Make this the project default connection."),
    ] = False,
) -> None:
    """Add a new source connection to canon.yaml."""
    json_output = get_cli_context(ctx).json_output
    root = _project_or_exit(ctx)

    params: dict[str, str] = {}
    for kv in param or []:
        if "=" not in kv:
            msg = f"--param must be KEY=VALUE, got {kv!r}"
            if json_output:
                typer.echo(json.dumps({"error": msg}))
            else:
                _console.print(f"[red]error:[/red] {msg}")
            raise typer.Exit(1)
        k, _, v = kv.partition("=")
        params[k] = v

    config_path = root / "canon.yaml"
    try:
        config = load_config(config_path)
    except ConfigError as exc:
        msg = str(exc)
        if json_output:
            typer.echo(json.dumps({"error": msg}))
        else:
            _console.print(f"[red]error:[/red] {msg}")
        raise typer.Exit(1) from exc

    if any(c.id == id_ for c in config.connections):
        msg = f"connection {id_!r} already exists; use 'connection remove' first to replace it"
        if json_output:
            typer.echo(json.dumps({"error": msg}))
        else:
            _console.print(f"[red]error:[/red] {msg}")
        raise typer.Exit(1)

    raw = _load_raw(config_path)
    new_entry: dict[str, Any] = {"id": id_, "type": type_}
    if params:
        new_entry["params"] = params
    if credentials_ref is not None:
        new_entry["credentials_ref"] = credentials_ref

    raw.setdefault("connections", [])
    raw["connections"].append(new_entry)
    if set_default:
        raw["project"]["default_connection"] = id_

    _write_raw(config_path, raw)

    if json_output:
        typer.echo(json.dumps({"added": id_, "default": set_default}))
    else:
        default_note = " (set as default)" if set_default else ""
        _console.print(f"[green]added[/green] connection [bold]{id_}[/bold]{default_note}")


@app.command("remove")
@handle_errors
def remove(
    ctx: typer.Context,
    id_: Annotated[str, typer.Argument(help="Connection id to remove.")],
) -> None:
    """Remove a configured connection from canon.yaml."""
    json_output = get_cli_context(ctx).json_output
    root = _project_or_exit(ctx)
    config_path = root / "canon.yaml"
    try:
        config = load_config(config_path)
    except ConfigError as exc:
        msg = str(exc)
        if json_output:
            typer.echo(json.dumps({"error": msg}))
        else:
            _console.print(f"[red]error:[/red] {msg}")
        raise typer.Exit(1) from exc

    if not any(c.id == id_ for c in config.connections):
        msg = f"connection {id_!r} not found"
        if json_output:
            typer.echo(json.dumps({"error": msg}))
        else:
            _console.print(f"[red]error:[/red] {msg}")
        raise typer.Exit(1)

    is_default = config.project.default_connection == id_
    raw = _load_raw(config_path)
    raw["connections"] = [c for c in raw.get("connections", []) if c["id"] != id_]
    if is_default:
        raw["project"].pop("default_connection", None)

    _write_raw(config_path, raw)

    if json_output:
        typer.echo(json.dumps({"removed": id_, "was_default": is_default}))
    else:
        default_note = " [yellow](was the default connection)[/yellow]" if is_default else ""
        _console.print(f"[green]removed[/green] connection [bold]{id_}[/bold]{default_note}")
