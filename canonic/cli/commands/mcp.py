"""``canonic mcp`` — MCP daemon control (E8 §4.2)."""

from __future__ import annotations

import json
from pathlib import Path  # noqa: TC003 — used in function return type (runtime)
from typing import Annotated, Literal

import typer
from rich.console import Console

from canonic.cli._errors import get_cli_context
from canonic.config import ConfigError, find_project_root, load_config

app = typer.Typer(name="mcp", help="Control the local MCP daemon.")
_console = Console(soft_wrap=True)

_LAST_PROJECT_FILE = Path.home() / ".canonic" / "last-project"


def _save_last_project(root: Path) -> None:
    _LAST_PROJECT_FILE.parent.mkdir(exist_ok=True)
    _LAST_PROJECT_FILE.write_text(str(root))


def _load_last_project() -> Path | None:
    if not _LAST_PROJECT_FILE.exists():
        return None
    p = Path(_LAST_PROJECT_FILE.read_text().strip())
    return p if (p / "canonic.yaml").exists() else None


def _resolve_root(ctx: typer.Context, explicit: Path | None) -> Path:
    json_output = get_cli_context(ctx).json_output

    if explicit is not None:
        resolved = explicit.resolve()
        if not (resolved / "canonic.yaml").exists():
            msg = f"no canonic.yaml found in {resolved}"
            if json_output:
                typer.echo(json.dumps({"error": msg}))
            else:
                _console.print(f"[red]error:[/red] {msg}")
            raise typer.Exit(1)
        return resolved

    root = find_project_root()
    if root is not None:
        return root

    root = _load_last_project()
    if root is not None:
        return root

    msg = "no canonic project found — use --project <path> or run from inside a project directory"
    if json_output:
        typer.echo(json.dumps({"error": msg}))
    else:
        _console.print(f"[red]error:[/red] {msg}")
    raise typer.Exit(1)


@app.command("start")
def start(
    ctx: typer.Context,
    project: Annotated[
        Path | None,
        typer.Option("--project", "-p", help="Path to canonic project root (overrides cwd walk)."),
    ] = None,
    transport: Annotated[
        Literal["stdio", "http"],
        typer.Option("--transport", help="Transport: 'stdio' (default, local) or 'http' (remote)."),
    ] = "stdio",
    port: Annotated[
        int, typer.Option("--port", help="Port for HTTP daemon (default 7474).")
    ] = 7474,
    host: Annotated[
        str, typer.Option("--host", help="Host for HTTP daemon (default 127.0.0.1).")
    ] = "127.0.0.1",
    token_ref: Annotated[
        str | None,
        typer.Option(
            "--token-ref",
            help=(
                "Bearer token reference (env:VAR) for --transport http, overriding/"
                "supplementing mcp.auth.tokens in canonic.yaml."
            ),
        ),
    ] = None,
    suggestions: Annotated[
        bool,
        typer.Option("--suggestions", help="Enable follow-up suggestions in query responses."),
    ] = False,
    child: Annotated[
        bool,
        typer.Option(
            "--_child",
            hidden=True,
            help="Internal: marks the already-detached http daemon process spawned by "
            "canonic.mcp.daemon.start_http. Do not pass this by hand.",
        ),
    ] = False,
) -> None:
    """Start the local MCP daemon.

    With ``--transport stdio`` (default): runs in the foreground (the MCP client
    manages the process lifetime). With ``--transport http``: spawns a detached
    background uvicorn daemon bound to the given host/port; requires at least one
    bearer token (``mcp.auth.tokens`` in canonic.yaml or ``--token-ref``) since the
    daemon becomes network-reachable (AMENDMENT-remote-mcp-transport.md).
    """
    root = _resolve_root(ctx, project)
    json_output = get_cli_context(ctx).json_output

    try:
        cfg = load_config(root / "canonic.yaml")
    except ConfigError as exc:
        msg = f"config error: {exc}"
        if json_output:
            typer.echo(json.dumps({"error": msg}))
        else:
            _console.print(f"[red]error:[/red] {msg}")
        raise typer.Exit(1) from exc

    from canonic.core.service import CanonicService
    from canonic.exc import CredentialError
    from canonic.mcp.auth import build_token_verifier
    from canonic.mcp.daemon import start_http, start_stdio

    try:
        service = CanonicService.from_project(root)
    except Exception as exc:
        msg = f"failed to load project context: {exc}"
        if json_output:
            typer.echo(json.dumps({"error": msg}))
        else:
            _console.print(f"[red]error:[/red] {msg}")
        raise typer.Exit(1) from exc

    _save_last_project(root)

    if not service.list_metrics():
        from canonic.contracts.bootstrap import write_inferred_contracts
        from canonic.semantic.loader import list_semantic_sources

        sources = list_semantic_sources(root)
        if sources:
            count = write_inferred_contracts(root, sources)
            if count:
                if not json_output:
                    _console.print(f"[dim]auto-generated {count} inferred metric contract(s)[/dim]")
                service = CanonicService.from_project(root)

    try:
        if transport == "http":
            try:
                auth = build_token_verifier(cfg.mcp.auth, extra_token_ref=token_ref)
            except CredentialError as exc:
                msg = f"could not resolve mcp auth token: {exc}"
                if json_output:
                    typer.echo(json.dumps({"error": msg}))
                else:
                    _console.print(f"[red]error:[/red] {msg}")
                raise typer.Exit(1) from exc
            if auth is None:
                msg = (
                    "http transport requires at least one bearer token — add "
                    "mcp.auth.tokens to canonic.yaml or pass --token-ref"
                )
                if json_output:
                    typer.echo(json.dumps({"error": msg}))
                else:
                    _console.print(f"[red]error:[/red] {msg}")
                raise typer.Exit(1)

            if child:
                # Already the detached process spawned by start_http (via `--_child`):
                # run in the foreground, don't spawn yet another child.
                from canonic.mcp.daemon import serve_http_foreground

                serve_http_foreground(service, root, host, port, auth=auth, suggestions=suggestions)
                return

            start_http(
                service,
                root,
                host=host,
                port=port,
                auth=auth,
                suggestions=suggestions,
                token_ref=token_ref,
            )
            if json_output:
                typer.echo(
                    json.dumps(
                        {"status": "started", "transport": "http", "host": host, "port": port}
                    )
                )
            else:
                _console.print(f"MCP daemon started (http {host}:{port})")
        else:
            start_stdio(service, root, suggestions=suggestions)
    except RuntimeError as exc:
        msg = str(exc)
        if json_output:
            typer.echo(json.dumps({"error": msg}))
        else:
            _console.print(f"[red]error:[/red] {msg}")
        raise typer.Exit(1) from exc


@app.command("stop")
def stop(ctx: typer.Context) -> None:
    """Stop the background MCP daemon."""
    root = _resolve_root(ctx, None)
    json_output = get_cli_context(ctx).json_output

    from canonic.mcp.daemon import stop as daemon_stop

    was_running = daemon_stop(root)
    if json_output:
        typer.echo(json.dumps({"stopped": was_running}))
    elif was_running:
        _console.print("MCP daemon stopped.")
    else:
        _console.print("MCP daemon was not running.")


@app.command("status")
def status(ctx: typer.Context) -> None:
    """Report whether the MCP daemon is running."""
    root = _resolve_root(ctx, None)
    json_output = get_cli_context(ctx).json_output

    from canonic.mcp.daemon import status as daemon_status

    s = daemon_status(root)

    if json_output:
        payload = {
            "running": s.running,
            "pid": s.pid,
            "version": s.version,
            "transport": s.transport,
            "host": s.host,
            "port": s.port,
            "started_at": s.started_at,
            "version_mismatch": s.version_mismatch,
            "auth_enabled": s.auth_enabled,
        }
        typer.echo(json.dumps(payload))
        return

    if not s.running:
        _console.print("MCP daemon: [bold]not running[/bold]")
        _console.print("Run [bold]canonic mcp start[/bold] to start it.")
        return

    _console.print(f"MCP daemon: [green]running[/green] (PID {s.pid})")
    _console.print(f"  transport: {s.transport}")
    if s.transport == "http":
        _console.print(f"  address:   {s.host}:{s.port}")
        _console.print(f"  auth:      {'token-protected' if s.auth_enabled else 'none'}")
    _console.print(f"  version:   {s.version}")
    _console.print(f"  started:   {s.started_at}")
    if s.version_mismatch:
        _console.print(
            f"  [yellow]warning:[/yellow] daemon version {s.version!r} differs from "
            f"CLI version {s.current_version!r} — run `canonic mcp stop && canonic mcp start`"
        )
