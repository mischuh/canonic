"""``canonic setup`` — interactive project setup wizard (SPEC E1 §4, OB-S1).

Bootstraps a new canonic project: name → first connection (test-gated) → LLM →
optional schema preview → write ``canonic.yaml`` + scaffold dirs + ``.gitignore``.
Progress is checkpointed to ``.canonic/setup-state.json`` so an interrupted run
resumes. Run inside an existing project it offers a status/add-connection menu
rather than overwriting committed files.

After the config is written, the golden path (OB-S1) runs steps 5–7:
  5. Bootstrap the first connection (tier-1 introspection → deterministic semantic sources).
  6. Run a first answer (demo metric query → result rows + metadata band).
  7. Hand off (what to review, exact query call, how to connect an agent).
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import os
from collections import Counter
from enum import IntEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import typer
from rich.panel import Panel
from rich.table import Table

from canonic.cli._errors import get_cli_context, handle_errors
from canonic.cli.commands import _console
from canonic.cli.setup_state import (
    STEP_CONNECTION,
    STEP_LLM,
    STEP_NAME,
    STEP_SCHEMA,
    SetupState,
    clear_state,
    load_state,
    save_state,
)
from canonic.config import (
    CanonicConfig,
    ConfigError,
    Connection,
    LLMConfig,
    ProjectConfig,
    TelemetryConfig,
    dump_config,
    load_config,
    scaffold_project,
)
from canonic.connectors.factory import default_factory
from canonic.contracts.bootstrap import write_inferred_contracts as _write_bootstrap_contracts
from canonic.contracts.models import CanonicalRef, MetricBinding, Status
from canonic.contracts.resolver import ContractResolver
from canonic.core.service import CanonicService
from canonic.exc import CanonicError, ConnectionError, CredentialError
from canonic.ingestion.models import DraftedBy
from canonic.instrumentation.events import DiskAnswerEventLog, emit_milestone
from canonic.instrumentation.models import FunnelMilestone
from canonic.llm_providers import PROVIDERS, CredentialMode
from canonic.semantic.loader import list_semantic_sources
from canonic.semantic.models import NormalizedType

if TYPE_CHECKING:
    from canonic.compiler.query import SemanticQuery
    from canonic.connectors.base import Health, RelationSchema, SchemaIntrospectable
    from canonic.core.models import QueryResult
    from canonic.ingestion.emitter import EmittedDiff
    from canonic.ingestion.pipeline import PipelineResult
    from canonic.semantic.models import Dimension, Measure, SemanticSource

_DEFAULT_TYPE = "postgres"
_DEMO_LIMIT = 10
_REVIEW_CAP = 5
_LOW_CARDINALITY_TYPES = frozenset(
    {NormalizedType.DATE, NormalizedType.TIMESTAMP, NormalizedType.BOOL}
)


class ReviewTier(IntEnum):
    """Priority tier for the curated first review (SPEC-onboarding §5, OB-S4).

    Lower value = higher priority = shown first: grains and FK-less joins both corrupt query
    correctness structurally (highest blast radius), then LLM-named measures, then the
    low-confidence long tail.
    """

    GRAIN = 0
    JOIN = 1
    MEASURE = 2
    LONG_TAIL = 3


@dataclasses.dataclass(frozen=True)
class _ReviewItem:
    target: str
    source_name: str
    tier: ReviewTier
    confidence: float
    anchors: list[str]
    why: str


_WHY_LINES: dict[ReviewTier, str] = {
    ReviewTier.GRAIN: "no primary key — grain is a guess; a wrong grain corrupts every measure here",
    ReviewTier.JOIN: (
        "no FK constraint — join target guessed from column-name convention; a wrong join "
        "silently duplicates or drops rows"
    ),
    ReviewTier.MEASURE: "LLM-named measure — confirm name/expr before trusting",
    ReviewTier.LONG_TAIL: "low-confidence inference — confirm before trusting",
}


def _classify_withheld(
    withheld: list[EmittedDiff],
    contents: dict[str, dict[str, Any]],
    ref_counts: dict[str, int],
) -> list[_ReviewItem]:
    """Classify and sort withheld diffs into teachable review items (SPEC-onboarding §5, OB-S4).

    Priority: grain drafts → FK-less join drafts → LLM-named measures → long tail.
    Within each tier, higher incoming-join count (blast radius) sorts first.
    """
    items: list[_ReviewItem] = []
    for diff in withheld:
        source_name = Path(diff.target).stem
        content = contents.get(diff.target, {})
        is_grain_draft = content.get("meta", {}).get("grain_draft") is True
        is_join_draft = content.get("meta", {}).get("join_draft") is True
        if is_grain_draft:
            tier = ReviewTier.GRAIN
        elif is_join_draft:
            tier = ReviewTier.JOIN
        elif diff.drafted_by is DraftedBy.LLM:
            tier = ReviewTier.MEASURE
        else:
            tier = ReviewTier.LONG_TAIL
        items.append(
            _ReviewItem(
                target=diff.target,
                source_name=source_name,
                tier=tier,
                confidence=diff.confidence,
                anchors=list(diff.anchored_to),
                why=_WHY_LINES[tier],
            )
        )
    items.sort(key=lambda item: (item.tier, -ref_counts.get(item.source_name, 0), item.target))
    return items


@handle_errors
def setup(ctx: typer.Context) -> None:
    """Run the interactive project setup wizard."""
    if get_cli_context(ctx).json_output:
        _console.print(
            "[red]error:[/red] setup is interactive — run `canonic setup` without --json"
        )
        raise typer.Exit(1)

    root = Path.cwd()
    if (root / "canonic.yaml").exists():
        _existing_project_menu(root)
        return
    _run_wizard(root)


def run_interactive() -> None:
    """Entry point for bare ``canonic``: wizard outside a project, menu inside one."""
    from canonic.config import find_project_root

    root = find_project_root()
    if root is not None:
        _existing_project_menu(root)
    else:
        _run_wizard(Path.cwd())


# --- fresh setup -----------------------------------------------------------


def _run_wizard(root: Path) -> None:
    state = load_state(root) or SetupState()
    if state.completed_steps:
        _console.print("[dim]resuming interrupted setup…[/dim]")
    else:
        emit_milestone(DiskAnswerEventLog(root), FunnelMilestone.SETUP_STARTED)

    if not state.done(STEP_NAME):
        state.project_name = typer.prompt("Project name", default=root.name)
        state.mark(STEP_NAME)
        save_state(root, state)

    if not state.done(STEP_CONNECTION):
        state.connection = _prompt_connection(root)
        state.mark(STEP_CONNECTION)
        save_state(root, state)
        emit_milestone(DiskAnswerEventLog(root), FunnelMilestone.CONNECTION_ADDED)

    if not state.done(STEP_LLM):
        state.llm = _prompt_llm()
        state.mark(STEP_LLM)
        save_state(root, state)

    if not state.done(STEP_SCHEMA):
        state.schema_previewed = _maybe_preview_schema(state.connection)
        state.mark(STEP_SCHEMA)
        save_state(root, state)

    assert state.project_name and state.connection and state.llm  # guarded by steps
    config = CanonicConfig(
        version=1,
        project=ProjectConfig(name=state.project_name, default_connection=state.connection.id),
        connections=[state.connection],
        llm=state.llm,
        telemetry=TelemetryConfig(),
    )
    created = scaffold_project(root)
    dump_config(config, root / "canonic.yaml")
    load_config(root / "canonic.yaml")  # assert the written file round-trips
    clear_state(root)
    _run_golden_path(root, config, created)


# --- golden path (OB-S1) ---------------------------------------------------


def _run_golden_path(root: Path, config: CanonicConfig, scaffolded: list[Path]) -> None:
    """Steps 5–7: bootstrap → first answer → handoff + completion panel."""
    _console.print("\n[dim]step 5 — bootstrapping connection…[/dim]")
    pipeline_result = _bootstrap_connection(root, config)
    if pipeline_result is not None:
        emit_milestone(DiskAnswerEventLog(root), FunnelMilestone.BOOTSTRAP_COMPLETED)

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

    withheld_count = _render_curated_review(pipeline_result)
    _render_setup_complete(config, scaffolded, demo_ok=demo_ok, withheld_count=withheld_count)


def _bootstrap_connection(root: Path, config: CanonicConfig) -> PipelineResult | None:
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
    root: Path, config: CanonicConfig, conn: Connection, conn_id: str
) -> PipelineResult:
    from canonic.ingestion.pipeline import IngestionPipeline

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

    from canonic.ingestion.pipeline import first_run_auto_acceptable

    accepted = sum(1 for d in result.emission.diffs if first_run_auto_acceptable(d))
    withheld = sum(1 for d in result.emission.diffs if not first_run_auto_acceptable(d))
    msg = f"[green]✓[/green] bootstrapped {accepted} semantic source(s)"
    if withheld:
        msg += f", [yellow]{withheld} held for review[/yellow] (no primary key — grain needs confirmation)"
    _console.print(msg)
    return result


def _render_curated_review(pipeline_result: PipelineResult | None) -> int:
    """Render the capped, prioritized curated review of withheld diffs (SPEC-onboarding §5, OB-S4).

    Returns the total withheld count (shown in the completion panel handoff).
    Shows at most ``_REVIEW_CAP`` items as teachable units (proposal + evidence anchor +
    confidence + why-line); the remainder is pointed at ``canonic ingest`` with a count.
    """
    if pipeline_result is None:
        return 0

    from canonic.ingestion.pipeline import first_run_auto_acceptable

    withheld = [d for d in pipeline_result.emission.diffs if not first_run_auto_acceptable(d)]
    if not withheld:
        return 0

    contents: dict[str, dict[str, Any]] = {
        entry.target: entry.proposal.content for entry in pipeline_result.emission.report.entries
    }
    ref_counts: dict[str, int] = Counter()
    for entry in pipeline_result.emission.report.entries:
        for join in entry.proposal.content.get("joins", []):
            if to := join.get("to"):
                ref_counts[to] += 1

    items = _classify_withheld(withheld, contents, ref_counts)
    shown = items[:_REVIEW_CAP]
    deferred = len(items) - len(shown)

    _console.print("\n[dim]curated review — sources held for human confirmation[/dim]")
    for item in shown:
        anchor = item.anchors[0] if item.anchors else "—"
        _console.print(
            f"  [yellow]·[/yellow] [bold]{item.source_name}[/bold]"
            f"  confidence={item.confidence:.1f}"
            f"  evidence={anchor}"
        )
        _console.print(f"    [dim]{item.why}[/dim]")
    if deferred:
        _console.print(
            f"  [dim]… and {deferred} more — run [bold]canonic ingest[/bold] to review them[/dim]"
        )

    return len(items)


def _try_first_answer(root: Path, config: CanonicConfig, sources: list[SemanticSource]) -> bool:
    """Attempt the demo query; return True if result rows were shown."""
    source, measure, dim = _pick_demo_target(sources)
    if source is None or measure is None:
        _console.print("\n[dim]step 6 — schema overview[/dim]")
        _render_describe_fallback(config, sources)
        return False

    _console.print("\n[dim]step 6 — running first answer…[/dim]")
    try:
        result, sq = asyncio.run(_run_demo_query(root, config, sources, source, measure, dim))
    except CanonicError as exc:  # structured registry-coded failure — surface it (OB-S5 AC2)
        _surface_demo_error(exc)
        _render_describe_fallback(config, sources)
        return False
    except Exception as exc:  # noqa: BLE001 — unexpected demo errors must not abort setup
        _console.print(f"[yellow]demo query failed:[/yellow] {exc}")
        _render_describe_fallback(config, sources)
        return False

    _render_first_answer(result, sq, source.name)
    emit_milestone(DiskAnswerEventLog(root), FunnelMilestone.FIRST_ANSWER_SERVED)
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
    root: Path,
    config: CanonicConfig,
    sources: list[SemanticSource],
    source: SemanticSource,
    measure: Measure,
    dim: Dimension | None,
) -> tuple[QueryResult, SemanticQuery]:
    """Synthetic in-memory binding → compile + execute through the normal path (OB-S1 AC1).

    ``root`` is passed so the demo answer writes a ``served_answer`` event, seeding the
    first accuracy/usage data (SPEC-onboarding §9/§10).
    """
    from canonic.compiler.query import SemanticQuery

    binding = MetricBinding(
        metric=measure.name,
        canonical=CanonicalRef(source=source.name, measure=measure.name),
        status=Status.ACTIVE,
    )
    service = CanonicService(
        config=config,
        resolver=ContractResolver(bindings=[binding], guardrails=[]),
        sources=sources,
        project_root=root,
        event_log=DiskAnswerEventLog(root),
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
    _console.print(f"\n[green]✓[/green] canonic found {len(sources)} semantic source(s):")
    for s in sources[:5]:
        _console.print(
            f"  [bold]{s.name}[/bold]"
            f" — {len(s.measures)} measure(s), {len(s.dimensions)} dimension(s)"
        )
    if len(sources) > 5:
        _console.print(
            f"  … and {len(sources) - 5} more (inspect [bold]semantics/[/bold] for all sources)"
        )


def _surface_demo_error(exc: CanonicError) -> None:
    """Surface a registry-coded demo-query failure (OB-S5 AC2 — never swallow it)."""
    code = exc.code.value if exc.code is not None else "internal_error"
    _console.print(f"[red]demo query failed[/red] [bold]{code}[/bold]: {exc}")


def _describe_service(
    config: CanonicConfig, sources: list[SemanticSource]
) -> CanonicService | None:
    """Build an in-memory service over all p0-compilable measures for the describe fallback."""
    bindings = [
        MetricBinding(
            metric=m.name,
            canonical=CanonicalRef(source=s.name, measure=m.name),
            status=Status.ACTIVE,
        )
        for s in sources
        for m in s.measures
        if m.is_p0_compilable
    ]
    if not bindings:
        return None
    return CanonicService(
        config=config,
        resolver=ContractResolver(bindings=bindings, guardrails=[]),
        sources=sources,
    )


def _render_describe_fallback(config: CanonicConfig, sources: list[SemanticSource]) -> None:
    """Describe-level ending when a full demo answer is not possible (SPEC §6/§7, OB-S5 AC2).

    Shows the shape of a question the user can ask: the available metrics and a description
    of the top metric's grain and dimensions.  Falls back to the plain source listing when
    nothing is describable (no p0-compilable measures).
    """
    service = _describe_service(config, sources)
    if service is None:
        _render_source_listing(sources)
        return
    metrics = service.list_metrics()
    if not metrics:
        _render_source_listing(sources)
        return
    _console.print(f"\n[green]✓[/green] canonic found {len(metrics)} metric(s) you can query:")
    for m in metrics[:_REVIEW_CAP]:
        _console.print(f"  [bold]{m.metric}[/bold]  [dim]({m.kind})[/dim]")
    if len(metrics) > _REVIEW_CAP:
        _console.print(f"  [dim]… and {len(metrics) - _REVIEW_CAP} more[/dim]")
    try:
        detail = service.describe_metric(metrics[0].metric)
    except CanonicError:
        return
    lines: list[str] = [f"[bold]{detail.metric}[/bold]"]
    if detail.grain:
        lines.append(f"  grain: {', '.join(detail.grain)}")
    if detail.dimensions:
        lines.append(f"  dimensions: {', '.join(d.name for d in detail.dimensions[:5])}")
    if detail.measures:
        lines.append(f"  measures: {', '.join(detail.measures[:5])}")
    _console.print(Panel("\n".join(lines), title="what you can ask", border_style="dim"))


def _render_setup_complete(
    config: CanonicConfig, scaffolded: list[Path], *, demo_ok: bool, withheld_count: int = 0
) -> None:
    """Print the completion panel with three concrete next actions (step 7)."""
    files = ", ".join(p.name for p in scaffolded) if scaffolded else "no new paths"
    ingest_label = (
        f"[bold]canonic ingest[/bold]                 — review {withheld_count} proposal(s) waiting"
        if withheld_count
        else "[bold]canonic ingest[/bold]                 — review and apply the proposed semantic context"
    )
    next_steps = (
        f"{ingest_label}\n"
        "[bold]canonic query -f <query.json>[/bold]  — run your own query\n"
        "[bold]canonic mcp start[/bold]              — connect an agent via MCP"
    )
    if not demo_ok:
        next_steps += (
            "\n\n[dim]tip:[/dim] add doc sources or a richer connection to unlock richer answers"
        )
    if not (config.llm and config.llm.model):
        next_steps += "\n\n[dim]note:[/dim] naming/prose enrichment is available once you add an LLM to canonic.yaml"
    _console.print(
        Panel.fit(
            f"[green]✓[/green] Project [bold]{config.project.name}[/bold] is ready.\n"
            f"Wrote canonic.yaml and scaffolded {files}.\n\n"
            f"[bold]step 7 — what's next[/bold]\n{next_steps}",
            title="setup complete",
        )
    )


# --- existing project ------------------------------------------------------


def _existing_project_menu(root: Path) -> None:
    _console.print("[yellow]canonic.yaml already exists[/yellow] — entering project menu.")
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
        _console.print("[yellow]no semantic sources found[/yellow] — run canonic ingest first")
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
        version: int | str = load_config(root / "canonic.yaml").version
    except ConfigError as exc:
        version = f"invalid ({exc})"
    dotcanonic = "present" if (root / ".canonic").is_dir() else "absent"
    _console.print(f"project root:   [bold]{root}[/bold]")
    _console.print(f"config version: {version}")
    _console.print(f".canonic/:        {dotcanonic}")


def _add_connection_to_existing(root: Path) -> None:
    conn = _prompt_connection(root)
    config = load_config(root / "canonic.yaml")
    existing = next((c for c in config.connections if c.id == conn.id), None)
    if existing is not None and not typer.confirm(
        f"connection {conn.id!r} already exists — replace it?", default=False
    ):
        _console.print("[dim]kept existing connection; nothing written.[/dim]")
        return
    config.connections = [c for c in config.connections if c.id != conn.id] + [conn]
    dump_config(config, root / "canonic.yaml")
    _console.print(f"[green]✓[/green] connection [bold]{conn.id}[/bold] added to canonic.yaml")


# --- shared prompts --------------------------------------------------------


def _prompt_connection(root: Path) -> Connection:
    """Prompt for connection type then collect type-specific params, test-gated."""
    _console.print(Panel.fit("Configure the first data connection.", title="connection"))
    _console.print(
        "  [bold][1][/bold] SQLite   — local .db file, no credentials, works offline [dim](recommended for a first try)[/dim]"
    )
    _console.print(
        "  [bold][2][/bold] DuckDB   — local .duckdb file, analytical workloads, no credentials"
    )
    _console.print("  [bold][3][/bold] Postgres — server-based, requires host/port/credentials")
    _console.print("  [bold][4][/bold] Redshift — Amazon Redshift, requires host/port/credentials")
    while True:
        choice = typer.prompt("Type [1=sqlite / 2=duckdb / 3=postgres / 4=redshift]", default="1")
        if choice == "1":
            conn = _prompt_sqlite_params()
        elif choice == "2":
            conn = _prompt_duckdb_params()
        elif choice == "3":
            conn = _prompt_postgres_params()
        elif choice == "4":
            conn = _prompt_redshift_params()
        else:
            _console.print("[red]enter 1, 2, 3 or 4[/red]")
            continue

        health = _test_connection(conn)
        if health is not None and health.status == "ok":
            _console.print("[green]✓[/green] connection test passed")
            if conn.type in ("postgres", "redshift"):
                conn = _maybe_narrow_schema(conn)
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


def _prompt_duckdb_params() -> Connection:
    """Collect params for a DuckDB connection (file path only, no credentials)."""
    conn_id = typer.prompt("Connection id", default="local_duckdb")
    path = typer.prompt("Path to .duckdb file")
    return Connection(id=conn_id, type="duckdb", params={"path": path})


def _prompt_postgres_params() -> Connection:
    """Collect params for a Postgres connection (server + credentials env var)."""
    conn_id = typer.prompt("Connection id", default="warehouse_pg")
    params: dict[str, object] = {
        "host": typer.prompt("Host", default="localhost"),
        "port": typer.prompt("Port", default=5432, type=int),
        "user": typer.prompt("User", default="postgres"),
        "dbname": typer.prompt("Database"),
    }
    env_var = typer.prompt(
        "Env var holding the password",
        default=f"CANONIC_{conn_id.upper()}_PASSWORD",
    )
    if not os.environ.get(env_var):
        _console.print(
            f"\n[yellow]note:[/yellow] [bold]{env_var}[/bold] is not set in your current shell.\n"
            f"  Before the connection test runs, open a new terminal tab and export it:\n"
            f"  [bold]export {env_var}=<your-password>[/bold]\n"
            "  Setup progress is saved — if you need to exit now, re-run [bold]canonic setup[/bold] and it will resume here.\n"
        )
    return Connection(
        id=conn_id,
        type="postgres",
        params=params,
        credentials_ref=f"env:{env_var}",
    )


def _prompt_redshift_params() -> Connection:
    """Collect params for a Redshift connection (server + credentials env var)."""
    conn_id = typer.prompt("Connection id", default="warehouse_rs")
    params: dict[str, object] = {
        "host": typer.prompt("Host"),
        "port": typer.prompt("Port", default=5439, type=int),
        "user": typer.prompt("User"),
        "dbname": typer.prompt("Database"),
    }
    env_var = typer.prompt(
        "Env var holding the password",
        default=f"CANONIC_{conn_id.upper()}_PASSWORD",
    )
    if not os.environ.get(env_var):
        _console.print(
            f"\n[yellow]note:[/yellow] [bold]{env_var}[/bold] is not set in your current shell.\n"
            f"  Before the connection test runs, open a new terminal tab and export it:\n"
            f"  [bold]export {env_var}=<your-password>[/bold]\n"
            "  Setup progress is saved — if you need to exit now, re-run [bold]canonic setup[/bold] and it will resume here.\n"
        )
    return Connection(
        id=conn_id,
        type="redshift",
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
                "  Progress is saved — Ctrl-C, set the var, then re-run [bold]canonic setup[/bold] to resume."
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
    provider_list = ", ".join(sorted(PROVIDERS))
    while True:
        provider = typer.prompt(f"Provider ({provider_list})", default="openai_compatible")
        spec = PROVIDERS.get(provider)
        if spec is not None:
            break
        _console.print(f"[red]unknown provider {provider!r} — choose one of: {provider_list}[/red]")

    base_url = None
    if spec.requires_base_url:
        base_url = typer.prompt("Base URL", default="http://localhost:11434/v1")

    model = typer.prompt("Model")

    api_key_ref = None
    if spec.credential_mode is CredentialMode.FORBIDDEN:
        _console.print(
            "[yellow]This provider authenticates itself outside canonic.yaml — no API key "
            "needed here. The first generation call walks you through it (e.g. a "
            "device-code flow in the browser); the resulting credential is then cached "
            "on disk and reused for later runs.[/yellow]"
        )
    else:
        required = spec.credential_mode is CredentialMode.REQUIRED
        label = f"Env var holding the API key{'' if required else ' (optional)'}"
        while True:
            api_key_env = typer.prompt(label, default="")
            if api_key_env or not required:
                break
            _console.print(f"[red]llm.api_key_ref is required for provider {provider!r}[/red]")
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


async def _introspect(conn: Connection) -> list[RelationSchema]:
    connector = default_factory.create(conn)
    try:
        return list(await cast("SchemaIntrospectable", connector).introspect_schema())
    finally:
        await connector.aclose()


def _maybe_narrow_schema(conn: Connection) -> Connection:
    """Offer to discover schemas/tables and narrow conn.params to the user's picks."""
    if not typer.confirm("Narrow down schemas/tables now?", default=True):
        return conn
    relations = _discover_relations(conn)
    if relations is None:
        return conn
    if not relations:
        _console.print("[dim]no relations found — nothing to narrow.[/dim]")
        return conn

    schemas = sorted({r.relation.split(".", 1)[0] for r in relations})
    selected_schemas = _prompt_select_schemas(schemas)
    if selected_schemas is not None:
        conn.params["schemas"] = selected_schemas
        relations = [r for r in relations if r.relation.split(".", 1)[0] in selected_schemas]

    selected_tables = _prompt_select_tables(relations)
    if selected_tables is not None:
        conn.params["tables"] = selected_tables

    return conn


def _discover_relations(conn: Connection) -> list[RelationSchema] | None:
    """Introspect conn (unfiltered) to discover what schemas/tables exist."""
    try:
        return asyncio.run(_introspect(conn))
    except (CredentialError, ConnectionError) as exc:
        _console.print(f"[yellow]schema discovery skipped:[/yellow] {exc}")
        return None


def _prompt_select_schemas(schemas: list[str]) -> list[str] | None:
    """Show a numbered list of schemas and prompt for a selection; None means 'all'."""
    table = Table(title="schemas")
    table.add_column("#", justify="right")
    table.add_column("schema")
    for i, name in enumerate(schemas, start=1):
        table.add_row(str(i), name)
    _console.print(table)
    while True:
        choice = typer.prompt("Select schemas (e.g. 1,3,5-7) or 'all'", default="all")
        if choice.strip().lower() == "all":
            return None
        try:
            indices = _parse_index_ranges(choice, len(schemas))
        except ValueError as exc:
            _console.print(f"[red]{exc}[/red]")
            continue
        if not indices:
            _console.print("[red]select at least one schema, or 'all'[/red]")
            continue
        return [schemas[i - 1] for i in sorted(indices)]


def _prompt_select_tables(relations: list[RelationSchema]) -> list[str] | None:
    """Show a numbered list of tables and prompt for a selection; None means 'all'."""
    if not relations or not typer.confirm("Narrow down to specific tables too?", default=False):
        return None
    names = [r.relation for r in relations]
    table = Table(title="tables")
    table.add_column("#", justify="right")
    table.add_column("table")
    for i, name in enumerate(names, start=1):
        table.add_row(str(i), name)
    _console.print(table)
    while True:
        choice = typer.prompt(
            "Select tables — indices/ranges (e.g. 1,3,5-7), glob patterns (e.g. fact_*), or 'all'",
            default="all",
        )
        if choice.strip().lower() == "all":
            return None
        try:
            selected = _parse_table_tokens(choice, names)
        except ValueError as exc:
            _console.print(f"[red]{exc}[/red]")
            continue
        if not selected:
            _console.print("[red]select at least one table, or 'all'[/red]")
            continue
        return selected


def _parse_index_ranges(text: str, count: int) -> set[int]:
    """Parse comma-separated 1-based indices/ranges (e.g. '1,3,5-7') into a validated set."""
    indices: set[int] = set()
    for raw_token in text.split(","):
        token = raw_token.strip()
        if not token:
            continue
        if "-" in token:
            start_s, _, end_s = token.partition("-")
            if not (start_s.isdigit() and end_s.isdigit()):
                raise ValueError(f"invalid range: {token!r}")
            start, end = int(start_s), int(end_s)
            if start > end:
                raise ValueError(f"invalid range: {token!r}")
            candidates: range | list[int] = range(start, end + 1)
        else:
            if not token.isdigit():
                raise ValueError(f"invalid index: {token!r}")
            candidates = [int(token)]
        for i in candidates:
            if not 1 <= i <= count:
                raise ValueError(f"index {i} out of range (1-{count})")
            indices.add(i)
    return indices


def _is_index_range(token: str) -> bool:
    """True when token is a bare index ('7') or index range ('5-7'), not a glob pattern."""
    if "-" in token:
        start, _, end = token.partition("-")
        return start.isdigit() and end.isdigit()
    return token.isdigit()


def _parse_table_tokens(text: str, names: list[str]) -> list[str]:
    """Parse comma-separated tokens: index/range tokens resolve to names; other
    tokens are kept verbatim as glob patterns (matched at introspection time)."""
    count = len(names)
    selected: list[str] = []
    seen: set[str] = set()
    for raw_token in text.split(","):
        token = raw_token.strip()
        if not token:
            continue
        if _is_index_range(token):
            for i in sorted(_parse_index_ranges(token, count)):
                name = names[i - 1]
                if name not in seen:
                    seen.add(name)
                    selected.append(name)
        elif token not in seen:
            seen.add(token)
            selected.append(token)
    return selected
