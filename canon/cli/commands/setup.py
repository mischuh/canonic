"""``canon setup`` — interactive project setup wizard (SPEC E1 §4, OB-S1).

Bootstraps a new canon project: name → first connection (test-gated) → LLM →
optional schema preview → write ``canon.yaml`` + scaffold dirs + ``.gitignore``.
Progress is checkpointed to ``.canon/setup-state.json`` so an interrupted run
resumes. Run inside an existing project it offers a status/add-connection menu
rather than overwriting committed files.

After the config is written, the golden path (OB-S1) runs steps 5–7:
  5. Bootstrap the first connection (tier-1 introspection → deterministic semantic sources).
  6. Run a first answer (demo metric query → result rows + metadata band).
  7. Hand off (what to review, exact query call, how to connect an agent).
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import TYPE_CHECKING, cast

import typer
from rich.panel import Panel
from rich.table import Table

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
from canon.connectors.factory import default_factory
from canon.contracts.bootstrap import write_inferred_contracts as _write_bootstrap_contracts
from canon.contracts.models import CanonicalRef, MetricBinding, Status
from canon.contracts.resolver import ContractResolver
from canon.core.service import CanonService
from canon.exc import ConnectionError, CredentialError
from canon.ingestion.models import ProposalOp
from canon.semantic.loader import list_semantic_sources
from canon.semantic.models import NormalizedType

if TYPE_CHECKING:
    from canon.compiler.query import SemanticQuery
    from canon.connectors.base import Health, SchemaIntrospectable
    from canon.core.models import QueryResult
    from canon.ingestion.pipeline import PipelineResult
    from canon.semantic.models import Dimension, Measure, SemanticSource

_DEFAULT_TYPE = "postgres"
_DEMO_LIMIT = 10
_LOW_CARDINALITY_TYPES = frozenset(
    {NormalizedType.DATE, NormalizedType.TIMESTAMP, NormalizedType.BOOL}
)


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
    _run_golden_path(root, config, created)


# --- golden path (OB-S1) ---------------------------------------------------


def _run_golden_path(root: Path, config: CanonConfig, scaffolded: list[Path]) -> None:
    """Steps 5–7: bootstrap → first answer → handoff + completion panel."""
    _console.print("\n[dim]step 5 — bootstrapping connection…[/dim]")
    pipeline_result = _bootstrap_connection(root, config)

    sources: list[SemanticSource] = []
    try:  # noqa: SIM105 — need to assign to sources, contextlib.suppress cannot do that
        sources = list_semantic_sources(root)
    except Exception:  # noqa: BLE001 — loading errors must not abort setup
        pass

    if sources:
        contract_count = _write_bootstrap_contracts(root, sources)
        if contract_count:
            _console.print(
                f"[green]✓[/green] wrote {contract_count} inferred metric contract(s) "
                "— MCP server will list them immediately"
            )

    demo_ok = False
    if sources:
        demo_ok = _try_first_answer(root, config, sources)
    elif pipeline_result is not None:
        _console.print(
            "[yellow]no queryable tables found[/yellow] — "
            "add a doc source or richer connection to unlock the first answer"
        )

    _render_setup_complete(config, scaffolded, demo_ok=demo_ok)


def _bootstrap_connection(root: Path, config: CanonConfig) -> PipelineResult | None:
    """Run tier-1 bootstrap on the default connection; return None on any failure."""
    if not config.connections:
        return None
    default_id = config.project.default_connection or config.connections[0].id
    conn = next((c for c in config.connections if c.id == default_id), None)
    if conn is None:
        return None
    try:
        return asyncio.run(_bootstrap_async(root, config, conn, default_id))
    except Exception as exc:  # noqa: BLE001 — bootstrap failures must not abort setup
        _console.print(f"[yellow]bootstrap skipped:[/yellow] {exc}")
        return None


async def _bootstrap_async(
    root: Path, config: CanonConfig, conn: Connection, conn_id: str
) -> PipelineResult:
    from canon.ingestion.pipeline import IngestionPipeline

    connector = default_factory.create(conn)
    pipeline = IngestionPipeline(
        root,
        {conn_id: connector},
        config.reconcile,
        headless=True,  # forces NullLLMDrafter — deterministic core only (OB-S2)
    )
    try:
        result = await pipeline.bootstrap(conn_id)
    finally:
        await connector.aclose()

    add_count = sum(1 for d in result.emission.diffs if d.op is ProposalOp.ADD)
    _console.print(f"[green]✓[/green] bootstrapped {add_count} semantic source(s)")
    return result


def _try_first_answer(root: Path, config: CanonConfig, sources: list[SemanticSource]) -> bool:
    """Attempt the demo query; return True if result rows were shown."""
    source, measure, dim = _pick_demo_target(sources)
    if source is None or measure is None:
        _console.print("\n[dim]step 6 — schema overview[/dim]")
        _render_source_listing(sources)
        return False

    _console.print("\n[dim]step 6 — running first answer…[/dim]")
    try:
        result, sq = asyncio.run(_run_demo_query(config, sources, source, measure, dim))
    except Exception as exc:  # noqa: BLE001 — demo errors must not abort setup
        _console.print(f"[yellow]demo query skipped:[/yellow] {exc}")
        _render_source_listing(sources)
        return False

    _render_first_answer(result, sq, source.name)
    return True


def _pick_demo_target(
    sources: list[SemanticSource],
) -> tuple[SemanticSource | None, Measure | None, Dimension | None]:
    """Deterministic selection: ≥1 p0-compilable measure; tiebreak most measures → name asc."""
    candidates = [s for s in sources if any(m.is_p0_compilable for m in s.measures)]
    if not candidates:
        return None, None, None
    candidates.sort(key=lambda s: (-sum(1 for m in s.measures if m.is_p0_compilable), s.name))
    source = candidates[0]
    measure = next(m for m in source.measures if m.is_p0_compilable)
    return source, measure, _best_dimension(source)


def _best_dimension(source: SemanticSource) -> Dimension | None:
    """Prefer DATE/TIMESTAMP/BOOLEAN-backed dimensions; fall back to the first dimension."""
    if not source.dimensions:
        return None
    col_by_name = {c.name: c for c in source.columns}
    for dim in source.dimensions:
        col = col_by_name.get(dim.column)
        if col is not None and col.type in _LOW_CARDINALITY_TYPES:
            return dim
    return source.dimensions[0]


async def _run_demo_query(
    config: CanonConfig,
    sources: list[SemanticSource],
    source: SemanticSource,
    measure: Measure,
    dim: Dimension | None,
) -> tuple[QueryResult, SemanticQuery]:
    """Synthetic in-memory binding → compile + execute through the normal path (OB-S1 AC1)."""
    from canon.compiler.query import SemanticQuery

    binding = MetricBinding(
        metric=measure.name,
        canonical=CanonicalRef(source=source.name, measure=measure.name),
        status=Status.ACTIVE,
    )
    service = CanonService(
        config=config,
        resolver=ContractResolver(bindings=[binding], guardrails=[]),
        sources=sources,
    )
    sq = SemanticQuery(
        metrics=[measure.name],
        dimensions=[dim.name] if dim is not None else [],
        limit=_DEMO_LIMIT,
    )
    return await service.query(sq), sq


def _render_first_answer(result: QueryResult, sq: SemanticQuery, source_name: str) -> None:
    """Render result rows, metadata band, and the exact query call."""
    rs = result.result
    table = Table(show_header=True, header_style="bold cyan", title=f"first answer — {source_name}")
    for col in rs.columns:
        table.add_column(col.name)
    for row in rs.rows:
        table.add_row(*(str(v) for v in row))
    _console.print(table)
    if rs.truncated:
        _console.print("[yellow]note:[/yellow] result truncated at connection row limit")

    meta = result.metadata
    band: list[str] = []
    resolved = meta.resolved.get("metrics", {})
    if resolved:
        band.append("[bold]resolved[/bold]")
        band.extend(f"  {k} → {v}" for k, v in resolved.items())
    if meta.freshness:
        band.append("[bold]freshness[/bold]")
        for f in meta.freshness:
            stale = " (stale)" if f.stale else ""
            band.append(f"  {f.source}: {f.last_validated_at or 'unknown'}{stale}")
    sq_json = json.dumps(sq.model_dump(mode="json", exclude_defaults=True), indent=2)
    band.append(f"[bold]query[/bold]\n{sq_json}")
    _console.print(Panel("\n".join(band), title="metadata", border_style="dim"))


def _render_source_listing(sources: list[SemanticSource]) -> None:
    """Show discovered sources when a full demo query is not possible."""
    _console.print(f"\n[green]✓[/green] canon found {len(sources)} semantic source(s):")
    for s in sources[:5]:
        _console.print(
            f"  [bold]{s.name}[/bold]"
            f" — {len(s.measures)} measure(s), {len(s.dimensions)} dimension(s)"
        )
    if len(sources) > 5:
        _console.print(
            f"  … and {len(sources) - 5} more (inspect [bold]semantics/[/bold] for all sources)"
        )


def _render_setup_complete(config: CanonConfig, scaffolded: list[Path], *, demo_ok: bool) -> None:
    """Print the completion panel with three concrete next actions (step 7)."""
    files = ", ".join(p.name for p in scaffolded) if scaffolded else "no new paths"
    next_steps = (
        "[bold]canon ingest[/bold]                 — review and apply the proposed semantic context\n"
        "[bold]canon query -f <query.json>[/bold]  — run your own query\n"
        "[bold]canon mcp start[/bold]              — connect an agent via MCP"
    )
    if not demo_ok:
        next_steps += (
            "\n\n[dim]tip:[/dim] add doc sources or a richer connection to unlock richer answers"
        )
    _console.print(
        Panel.fit(
            f"[green]✓[/green] Project [bold]{config.project.name}[/bold] is ready.\n"
            f"Wrote canon.yaml and scaffolded {files}.\n\n"
            f"[bold]step 7 — what's next[/bold]\n{next_steps}",
            title="setup complete",
        )
    )


# --- existing project ------------------------------------------------------


def _existing_project_menu(root: Path) -> None:
    _console.print("[yellow]canon.yaml already exists[/yellow] — entering project menu.")
    _print_status(root)
    while True:
        choice = typer.prompt(
            "Select  [1] status  [2] add connection  [3] generate contracts  [4] exit",
            default="4",
        )
        if choice == "1":
            _print_status(root)
        elif choice == "2":
            _add_connection_to_existing(root)
        elif choice == "3":
            _generate_contracts_for_existing(root)
        elif choice == "4":
            return
        else:
            _console.print("[red]invalid choice[/red] — enter 1, 2, 3 or 4")


def _generate_contracts_for_existing(root: Path) -> None:
    """Load semantic sources from an existing project and write inferred contracts."""
    sources: list[SemanticSource] = []
    try:
        sources = list_semantic_sources(root)
    except Exception as exc:  # noqa: BLE001
        _console.print(f"[red]error loading sources:[/red] {exc}")
        return
    if not sources:
        _console.print("[yellow]no semantic sources found[/yellow] — run canon ingest first")
        return
    count = _write_bootstrap_contracts(root, sources)
    if count:
        _console.print(
            f"[green]✓[/green] wrote {count} inferred metric contract(s) "
            "— restart the MCP server to pick them up"
        )
    else:
        _console.print(
            "[dim]no new contracts to write — all already exist or no sources have numeric columns[/dim]"
        )


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
    """Prompt for connection type then collect type-specific params, test-gated."""
    _console.print(Panel.fit("Configure the first data connection.", title="connection"))
    _console.print(
        "  [bold][1][/bold] SQLite   — local .db file, no credentials, works offline [dim](recommended for a first try)[/dim]"
    )
    _console.print("  [bold][2][/bold] Postgres — server-based, requires host/port/credentials")
    while True:
        choice = typer.prompt("Type [1=sqlite / 2=postgres]", default="1")
        if choice == "1":
            conn = _prompt_sqlite_params()
        elif choice == "2":
            conn = _prompt_postgres_params()
        else:
            _console.print("[red]enter 1 or 2[/red]")
            continue

        health = _test_connection(conn)
        if health is not None and health.status == "ok":
            _console.print("[green]✓[/green] connection test passed")
            return conn

        if health is not None:
            _console.print(f"[red]connection test failed:[/red] {health.message}")
        if not typer.confirm("Try again?", default=True):
            raise typer.Exit(1)


def _prompt_sqlite_params() -> Connection:
    """Collect params for a SQLite connection (file path only, no credentials)."""
    conn_id = typer.prompt("Connection id", default="local_sqlite")
    path = typer.prompt("Path to .db file")
    return Connection(id=conn_id, type="sqlite", params={"path": path})


def _prompt_postgres_params() -> Connection:
    """Collect params for a Postgres connection (server + credentials env var)."""
    conn_id = typer.prompt("Connection id", default="warehouse_pg")
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
    if not os.environ.get(env_var):
        _console.print(
            f"\n[yellow]note:[/yellow] [bold]{env_var}[/bold] is not set in your current shell.\n"
            f"  Before the connection test runs, open a new terminal tab and export it:\n"
            f"  [bold]export {env_var}=<your-password>[/bold]\n"
            "  Setup progress is saved — if you need to exit now, re-run [bold]canon setup[/bold] and it will resume here.\n"
        )
    return Connection(
        id=conn_id,
        type="postgres",
        params=params,
        credentials_ref=f"env:{env_var}",
    )


def _test_connection(conn: Connection) -> Health | None:
    """Run the async connection test; return None when the test could not run."""
    try:
        return asyncio.run(_probe(conn))
    except CredentialError as exc:
        _console.print(f"[red]credential error:[/red] {exc}")
        if conn.credentials_ref and conn.credentials_ref.startswith("env:"):
            env_var = conn.credentials_ref[4:]
            _console.print(
                f"  Set it now:  [bold]export {env_var}=<your-password>[/bold]\n"
                "  Progress is saved — Ctrl-C, set the var, then re-run [bold]canon setup[/bold] to resume."
            )
        return None
    except ConnectionError as exc:
        _console.print(f"[red]cannot build connector:[/red] {exc}")
        return None


async def _probe(conn: Connection) -> Health:
    connector = default_factory.create(conn)
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
    connector = default_factory.create(conn)
    try:
        return list(await cast("SchemaIntrospectable", connector).introspect_schema())
    finally:
        await connector.aclose()
