"""``canon setup`` — interactive project setup wizard (SPEC E1 §4).

Bootstraps a new canon project: name → first connection (test-gated) → LLM →
optional schema preview → write ``canon.yaml`` + scaffold dirs + ``.gitignore``.
Progress is checkpointed to ``.canon/setup-state.json`` so an interrupted run
resumes. Run inside an existing project it offers a status/add-connection menu
rather than overwriting committed files.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING, cast

import typer
from rich.panel import Panel

from canon.cli._errors import get_cli_context, handle_errors
from canon.cli.commands import _console
from canon.cli.setup_state import (
    STEP_CONNECTION,
    STEP_LLM,
    STEP_NAME,
    STEP_SCHEMA,
    SetupState,
    clear_state,
    load_state,
    save_state,
)
from canon.config import (
    CanonConfig,
    ConfigError,
    Connection,
    LLMConfig,
    ProjectConfig,
    TelemetryConfig,
    dump_config,
    load_config,
    scaffold_project,
)
from canon.connectors.factory import connector_for
from canon.exc import ConnectionError, CredentialError

if TYPE_CHECKING:
    from canon.connectors.base import Health, SchemaIntrospectable

_DEFAULT_TYPE = "postgres"


@handle_errors
def setup(ctx: typer.Context) -> None:
    """Run the interactive project setup wizard."""
    if get_cli_context(ctx).json_output:
        _console.print("[red]error:[/red] setup is interactive — run `canon setup` without --json")
        raise typer.Exit(1)

    root = Path.cwd()
    if (root / "canon.yaml").exists():
        _existing_project_menu(root)
        return
    _run_wizard(root)


# --- fresh setup -----------------------------------------------------------


def _run_wizard(root: Path) -> None:
    state = load_state(root) or SetupState()
    if state.completed_steps:
        _console.print("[dim]resuming interrupted setup…[/dim]")

    if not state.done(STEP_NAME):
        state.project_name = typer.prompt("Project name", default=root.name)
        state.mark(STEP_NAME)
        save_state(root, state)

    if not state.done(STEP_CONNECTION):
        state.connection = _prompt_connection(root)
        state.mark(STEP_CONNECTION)
        save_state(root, state)

    if not state.done(STEP_LLM):
        state.llm = _prompt_llm()
        state.mark(STEP_LLM)
        save_state(root, state)

    if not state.done(STEP_SCHEMA):
        state.schema_previewed = _maybe_preview_schema(state.connection)
        state.mark(STEP_SCHEMA)
        save_state(root, state)

    assert state.project_name and state.connection and state.llm  # guarded by steps
    config = CanonConfig(
        version=1,
        project=ProjectConfig(name=state.project_name, default_connection=state.connection.id),
        connections=[state.connection],
        llm=state.llm,
        telemetry=TelemetryConfig(),
    )
    created = scaffold_project(root)
    dump_config(config, root / "canon.yaml")
    load_config(root / "canon.yaml")  # assert the written file round-trips
    clear_state(root)

    _console.print(
        Panel.fit(
            f"[green]✓[/green] Project [bold]{config.project.name}[/bold] is ready.\n"
            f"Wrote canon.yaml and scaffolded "
            f"{', '.join(p.name for p in created) or 'no new paths'}.",
            title="setup complete",
        )
    )


# --- existing project ------------------------------------------------------


def _existing_project_menu(root: Path) -> None:
    _console.print("[yellow]canon.yaml already exists[/yellow] — entering project menu.")
    _print_status(root)
    while True:
        choice = typer.prompt("Select  [1] status  [2] add connection  [3] exit", default="3")
        if choice == "1":
            _print_status(root)
        elif choice == "2":
            _add_connection_to_existing(root)
        elif choice == "3":
            return
        else:
            _console.print("[red]invalid choice[/red] — enter 1, 2 or 3")


def _print_status(root: Path) -> None:
    try:
        version: int | str = load_config(root / "canon.yaml").version
    except ConfigError as exc:
        version = f"invalid ({exc})"
    dotcanon = "present" if (root / ".canon").is_dir() else "absent"
    _console.print(f"project root:   [bold]{root}[/bold]")
    _console.print(f"config version: {version}")
    _console.print(f".canon/:        {dotcanon}")


def _add_connection_to_existing(root: Path) -> None:
    conn = _prompt_connection(root)
    config = load_config(root / "canon.yaml")
    existing = next((c for c in config.connections if c.id == conn.id), None)
    if existing is not None and not typer.confirm(
        f"connection {conn.id!r} already exists — replace it?", default=False
    ):
        _console.print("[dim]kept existing connection; nothing written.[/dim]")
        return
    config.connections = [c for c in config.connections if c.id != conn.id] + [conn]
    dump_config(config, root / "canon.yaml")
    _console.print(f"[green]✓[/green] connection [bold]{conn.id}[/bold] added to canon.yaml")


# --- shared prompts --------------------------------------------------------


def _prompt_connection(root: Path) -> Connection:
    """Prompt for and test a connection, returning it only once the test passes."""
    _console.print(Panel.fit("Configure the first data connection.", title="connection"))
    while True:
        conn_id = typer.prompt("Connection id", default="warehouse_pg")
        conn_type = typer.prompt("Type", default=_DEFAULT_TYPE)
        params: dict[str, object] = {
            "host": typer.prompt("Host", default="localhost"),
            "port": typer.prompt("Port", default=5432, type=int),
            "user": typer.prompt("User", default="postgres"),
            "dbname": typer.prompt("Database"),
        }
        schema = typer.prompt("Schema (optional)", default="")
        if schema:
            params["schema"] = schema

        env_var = typer.prompt(
            "Env var holding the password",
            default=f"CANON_{conn_id.upper()}_PASSWORD",
        )
        conn = Connection(
            id=conn_id,
            type=conn_type,
            params=params,
            credentials_ref=f"env:{env_var}",
        )

        health = _test_connection(conn, env_var)
        if health is not None and health.status == "ok":
            _console.print("[green]✓[/green] connection test passed")
            return conn

        if health is not None:
            _console.print(f"[red]connection test failed:[/red] {health.message}")
        if not typer.confirm("Try again?", default=True):
            raise typer.Exit(1)


def _test_connection(conn: Connection, env_var: str) -> Health | None:
    """Run the async connection test; return None when the test could not run."""
    try:
        return asyncio.run(_probe(conn))
    except CredentialError:
        _console.print(f"[red]env var {env_var!r} is not set[/red] — export it, then retry.")
        return None
    except ConnectionError as exc:
        _console.print(f"[red]cannot build connector:[/red] {exc}")
        return None


async def _probe(conn: Connection) -> Health:
    connector = connector_for(conn)
    try:
        return await connector.test_connection()
    finally:
        await connector.aclose()


def _prompt_llm() -> LLMConfig:
    _console.print(Panel.fit("Configure the language model.", title="llm"))
    provider = typer.prompt("Provider", default="openai_compatible")
    base_url = typer.prompt("Base URL", default="http://localhost:11434/v1")
    model = typer.prompt("Model")
    api_key_env = typer.prompt("Env var holding the API key (optional)", default="")
    api_key_ref = f"env:{api_key_env}" if api_key_env else None
    return LLMConfig(provider=provider, base_url=base_url, model=model, api_key_ref=api_key_ref)


def _maybe_preview_schema(conn: Connection | None) -> bool:
    if conn is None or not typer.confirm("Preview the schema now?", default=False):
        return False
    try:
        relations = asyncio.run(_introspect(conn))
    except (CredentialError, ConnectionError) as exc:
        _console.print(f"[yellow]schema preview skipped:[/yellow] {exc}")
        return False
    _console.print(f"[green]✓[/green] found {len(relations)} relations")
    return True


async def _introspect(conn: Connection) -> list[object]:
    connector = connector_for(conn)
    try:
        return list(await cast("SchemaIntrospectable", connector).introspect_schema())
    finally:
        await connector.aclose()
