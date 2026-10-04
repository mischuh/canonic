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
import logging
import os
from collections import Counter
from enum import IntEnum
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any

import typer
from rich.panel import Panel
from rich.table import Table

from canonic.cli._errors import get_cli_context, handle_errors
from canonic.cli.commands import _console, load_raw_config, write_raw_config
from canonic.cli.commands._schema_selection import (
    discover_relations,
    introspect_connection,
    prompt_select_schemas,
    prompt_select_tables,
)
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
from canonic.exc import CanonicError, ConnectionError, CredentialError, PackError
from canonic.ingestion.models import DraftedBy
from canonic.instrumentation.events import DiskAnswerEventLog, emit_milestone
from canonic.instrumentation.models import FunnelMilestone
from canonic.llm_providers import PROVIDERS, CredentialMode
from canonic.semantic.loader import list_semantic_sources
from canonic.semantic.models import NormalizedType

if TYPE_CHECKING:
    from collections.abc import Callable

    from canonic.compiler.query import SemanticQuery
    from canonic.connectors.base import Health
    from canonic.core.models import QueryResult
    from canonic.ingestion.emitter import EmittedDiff
    from canonic.ingestion.pipeline import PipelineResult
    from canonic.packs.manifest import PackManifest, Variant
    from canonic.semantic.models import Dimension, Measure, SemanticSource

logger = logging.getLogger(__name__)

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
def setup(
    ctx: typer.Context,
    minimal: Annotated[
        bool,
        typer.Option(
            "--minimal",
            "--bare",
            help=(
                "Zero-prompt scaffold-only setup: writes canonic.yaml (name only, no "
                "connections/LLM) plus the project directories, skips every prompt and "
                "the golden path. Add a connection/LLM later."
            ),
        ),
    ] = False,
) -> None:
    """Run the interactive project setup wizard."""
    if get_cli_context(ctx).json_output:
        _console.print(
            "[red]error:[/red] setup is interactive — run `canonic setup` without --json"
        )
        raise typer.Exit(1)

    root = Path.cwd()
    if (root / "canonic.yaml").exists():
        if minimal:
            _console.print(
                "[yellow]canonic.yaml already exists[/yellow]; "
                "--minimal only scaffolds a new project."
            )
            raise typer.Exit(1)
        _existing_project_menu(root)
        return
    if minimal:
        _run_minimal_setup(root)
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
    fresh_run = not state.completed_steps
    if state.completed_steps:
        _console.print("[dim]resuming interrupted setup…[/dim]")
        logger.info("setup: resuming interrupted run, completed_steps=%s", state.completed_steps)
    else:
        logger.info("setup: starting wizard at %s", root)
        emit_milestone(DiskAnswerEventLog(root), FunnelMilestone.SETUP_STARTED)

    if not state.done(STEP_NAME):
        state.project_name = typer.prompt("Project name", default=root.name)
        state.mark(STEP_NAME)
        save_state(root, state)
        logger.debug("setup: step name complete: %s", state.project_name)

    # §5.1: offer known-system packs before the generic connection/bootstrap path. Only on
    # a genuinely fresh run — pack-branch resumability is out of scope for v1 (a pack
    # install is idempotent by design, §2.3, so re-running `canonic setup` from scratch is
    # the supported recovery path for an interrupted pack install).
    assert state.project_name  # guarded by STEP_NAME above
    if fresh_run and _run_pack_branch(root, state.project_name):
        return

    if not state.done(STEP_CONNECTION):
        state.connection = _prompt_connection_or_skip(root)
        state.mark(STEP_CONNECTION)
        save_state(root, state)
        if state.connection is not None:
            logger.debug(
                "setup: step connection complete: id=%s type=%s",
                state.connection.id,
                state.connection.type,
            )
            emit_milestone(DiskAnswerEventLog(root), FunnelMilestone.CONNECTION_ADDED)
        else:
            logger.debug("setup: step connection skipped")

    if not state.done(STEP_LLM):
        state.llm = _prompt_llm_or_skip()
        state.mark(STEP_LLM)
        save_state(root, state)
        logger.debug("setup: step llm complete: configured=%s", state.llm is not None)

    if not state.done(STEP_SCHEMA):
        state.schema_previewed = _maybe_preview_schema(state.connection)
        state.mark(STEP_SCHEMA)
        save_state(root, state)
        logger.debug("setup: step schema preview complete: previewed=%s", state.schema_previewed)

    assert state.project_name  # guarded by STEP_NAME
    config = CanonicConfig(
        version=1,
        project=ProjectConfig(
            name=state.project_name,
            default_connection=state.connection.id if state.connection else None,
        ),
        connections=[state.connection] if state.connection else [],
        llm=state.llm,
        telemetry=TelemetryConfig(),
    )
    created = scaffold_project(root)
    dump_config(config, root / "canonic.yaml")
    load_config(root / "canonic.yaml")  # assert the written file round-trips
    clear_state(root)
    logger.info("setup: wrote canonic.yaml, scaffolded %d path(s)", len(created))
    _run_golden_path(root, config, created)


def _run_minimal_setup(root: Path) -> None:
    """Zero-prompt scaffold-only setup (``--minimal``/``--bare``).

    Skips every prompt and the golden path (bootstrap/demo query) entirely: writes a bare
    ``canonic.yaml`` (project name only — no connections, no LLM) plus the scaffolded
    directories, then reuses the same completion panel the full wizard ends with so the
    next steps (add a connection, add an LLM) are discoverable in one place. Bypasses
    ``SetupState``/checkpointing entirely — there is nothing to resume.
    """
    logger.info("setup: running --minimal scaffold-only setup at %s", root)
    config = CanonicConfig(
        version=1, project=ProjectConfig(name=root.name), telemetry=TelemetryConfig()
    )
    created = scaffold_project(root)
    dump_config(config, root / "canonic.yaml")
    load_config(root / "canonic.yaml")  # assert the written file round-trips
    clear_state(root)  # defensive: drop any stale checkpoint from a prior interrupted full run
    logger.info("setup: --minimal wrote canonic.yaml, scaffolded %d path(s)", len(created))
    _render_setup_complete(config, created, demo_ok=False, withheld_count=0)


# --- golden path (OB-S1) ---------------------------------------------------


def _confirm_generate_contracts() -> bool:
    """Ask whether to write inferred metric contracts now (isolated so tests can stub it)."""
    return typer.confirm("Generate metric contracts now?", default=True)


def _run_golden_path(root: Path, config: CanonicConfig, scaffolded: list[Path]) -> None:
    """Steps 5–7: bootstrap → first answer → handoff + completion panel."""
    if config.connections:
        _console.print("\n[dim]step 5: bootstrapping connection…[/dim]")
        logger.info("setup: golden path step 5: bootstrapping connection")
    else:
        _console.print("\n[dim]no connection configured — skipping bootstrap and demo query.[/dim]")
        logger.info("setup: golden path: no connection configured, skipping bootstrap")
    pipeline_result = _bootstrap_connection(root, config)
    if pipeline_result is not None:
        emit_milestone(DiskAnswerEventLog(root), FunnelMilestone.BOOTSTRAP_COMPLETED)

    sources: list[SemanticSource] = []
    try:  # noqa: SIM105 — need to assign to sources, contextlib.suppress cannot do that
        sources = list_semantic_sources(root)
    except Exception:  # noqa: BLE001 — loading errors must not abort setup
        logger.warning("setup: failed to load semantic sources after bootstrap", exc_info=True)

    if sources and _confirm_generate_contracts():
        contract_count = _write_bootstrap_contracts(root, sources)
        if contract_count:
            logger.info("setup: wrote %d inferred metric contract(s)", contract_count)
            _console.print(
                f"[green]✓[/green] wrote {contract_count} inferred metric contract(s) "
                "— MCP server will list them immediately"
            )
    elif sources:
        logger.info("setup: metric contract generation skipped")
        _console.print(
            "[dim]skipping metric contracts — generate them later via `canonic setup` "
            "→ [3] generate contracts.[/dim]"
        )

    demo_ok = False
    if sources:
        demo_ok = _try_first_answer(root, config, sources)
    elif pipeline_result is not None:
        logger.info("setup: no queryable tables found after bootstrap")
        _console.print(
            "[yellow]no queryable tables found[/yellow] — "
            "add a doc source or richer connection to unlock the first answer"
        )

    withheld_count = _render_curated_review(pipeline_result)
    logger.info("setup: complete demo_ok=%s withheld=%d", demo_ok, withheld_count)
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
        logger.warning("setup: bootstrap skipped: %s", exc, exc_info=True)
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
    logger.info("setup: bootstrap accepted=%d withheld=%d", accepted, withheld)
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

    _console.print("\n[dim]curated review: sources held for human confirmation[/dim]")
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
            f"  [dim]… and {deferred} more; run [bold]canonic ingest[/bold] to review them[/dim]"
        )

    return len(items)


def _try_first_answer(root: Path, config: CanonicConfig, sources: list[SemanticSource]) -> bool:
    """Attempt the demo query; return True if result rows were shown."""
    source, measure, dim = _pick_demo_target(sources)
    if source is None or measure is None:
        _console.print("\n[dim]step 6: schema overview[/dim]")
        _render_describe_fallback(config, sources)
        return False

    _console.print("\n[dim]step 6: running first answer…[/dim]")
    logger.info("setup: golden path step 6: running demo query on source=%s", source.name)
    try:
        result, sq = asyncio.run(_run_demo_query(root, config, sources, source, measure, dim))
    except CanonicError as exc:  # structured registry-coded failure — surface it (OB-S5 AC2)
        logger.warning("setup: demo query failed: %s", exc)
        _surface_demo_error(exc)
        _render_describe_fallback(config, sources)
        return False
    except Exception as exc:  # noqa: BLE001 — unexpected demo errors must not abort setup
        logger.warning("setup: demo query failed: %s", exc, exc_info=True)
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
    for dim in source.dimensions:
        if dim.value_type(source.columns) in _LOW_CARDINALITY_TYPES:
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
        event_log=DiskAnswerEventLog(root, config.instrumentation.events),
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
    table = Table(show_header=True, header_style="bold cyan", title=f"first answer: {source_name}")
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
        f"[bold]canonic ingest[/bold]                 : review {withheld_count} proposal(s) waiting"
        if withheld_count
        else "[bold]canonic ingest[/bold]                 : review and apply the proposed semantic context"
    )
    next_steps = (
        f"{ingest_label}\n"
        "[bold]canonic query --metrics <m> --dimensions <d>[/bold]  : run your own query\n"
        "[bold]canonic mcp start[/bold]              : connect an agent via MCP"
    )
    if not config.connections:
        next_steps += (
            "\n\n[dim]tip:[/dim] no connection configured — run [bold]canonic setup[/bold] "
            "again to add one, or edit canonic.yaml directly"
        )
    elif not demo_ok:
        next_steps += (
            "\n\n[dim]tip:[/dim] add doc sources or a richer connection to unlock richer answers"
        )
    if not (config.llm and config.llm.model):
        next_steps += "\n\n[dim]note:[/dim] naming/prose enrichment is available once you add an LLM to canonic.yaml"
    _console.print(
        Panel.fit(
            f"[green]✓[/green] Project [bold]{config.project.name}[/bold] is ready.\n"
            f"Wrote canonic.yaml and scaffolded {files}.\n\n"
            f"[bold]step 7: what's next[/bold]\n{next_steps}",
            title="setup complete",
        )
    )


# --- existing project ------------------------------------------------------


def _existing_project_menu(root: Path) -> None:
    _console.print("[yellow]canonic.yaml already exists[/yellow]; entering project menu.")
    _print_status(root)
    while True:
        choice = typer.prompt(
            "Select  [1] status  [2] add connection  [3] generate contracts  "
            "[4] configure LLM  [5] add a context pack  [6] exit",
            default="6",
        )
        if choice == "1":
            _print_status(root)
        elif choice == "2":
            _add_connection_to_existing(root)
        elif choice == "3":
            _generate_contracts_for_existing(root)
        elif choice == "4":
            _add_llm_to_existing(root)
        elif choice == "5":
            _add_pack_to_existing(root)
        elif choice == "6":
            return
        else:
            _console.print("[red]invalid choice[/red]; enter 1, 2, 3, 4, 5 or 6")


def _generate_contracts_for_existing(root: Path) -> None:
    """Load semantic sources from an existing project and write inferred contracts."""
    sources: list[SemanticSource] = []
    try:
        sources = list_semantic_sources(root)
    except Exception as exc:  # noqa: BLE001
        logger.warning("setup: failed to load semantic sources: %s", exc, exc_info=True)
        _console.print(f"[red]error loading sources:[/red] {exc}")
        return
    if not sources:
        _console.print("[yellow]no semantic sources found[/yellow]; run canonic ingest first")
        return
    count = _write_bootstrap_contracts(root, sources)
    if count:
        logger.info("setup: wrote %d inferred metric contract(s) for existing project", count)
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
    path = root / "canonic.yaml"
    load_config(path)  # fail early on an invalid file
    # Edit the raw document, not the loaded config: loading resolves ``env:VAR`` values and
    # writing those back would bake host/user into the file.
    raw = load_raw_config(path)
    connections = raw.get("connections") or []
    existing = next((c for c in connections if c.get("id") == conn.id), None)
    if existing is not None and not typer.confirm(
        f"connection {conn.id!r} already exists — replace it?", default=False
    ):
        _console.print("[dim]kept existing connection; nothing written.[/dim]")
        return
    kept = [c for c in connections if c.get("id") != conn.id]
    raw["connections"] = [*kept, conn.model_dump(mode="json", exclude_none=True)]
    write_raw_config(path, raw)
    logger.info("setup: connection %s (%s) added to canonic.yaml", conn.id, conn.type)
    _console.print(f"[green]✓[/green] connection [bold]{conn.id}[/bold] added to canonic.yaml")


def _add_llm_to_existing(root: Path) -> None:
    """Configure or replace the LLM block on an existing project (mirrors _add_connection_to_existing)."""
    llm = _prompt_llm()
    path = root / "canonic.yaml"
    load_config(path)  # fail early on an invalid file
    raw = load_raw_config(path)
    if raw.get("llm") is not None and not typer.confirm(
        "an LLM is already configured — replace it?", default=False
    ):
        _console.print("[dim]kept existing LLM config; nothing written.[/dim]")
        return
    raw["llm"] = llm.model_dump(mode="json", exclude_none=True, exclude_defaults=False)
    if not raw["llm"].get("tasks"):
        raw["llm"].pop("tasks", None)
    write_raw_config(path, raw)
    logger.info("setup: llm provider=%s model=%s added to canonic.yaml", llm.provider, llm.model)
    _console.print(
        f"[green]✓[/green] LLM [bold]{llm.provider}/{llm.model}[/bold] added to canonic.yaml"
    )


# --- context packs (AMENDMENT-context-packs §5) -----------------------------
#
# §5.5's flow order: pick pack+variant → connection (§5.2) → location check (§5.3) →
# params (§5.4) → preview+confirm (§5.5) → write (§2.3) → first answer (§5.6). This is a
# second entry point into the same ``canonic.packs.install``/``_pack_prompts`` machinery
# ``canonic pack add`` uses (canonic/cli/commands/pack.py) — no separate implementation.

_DEFAULT_PACKS_REPO_ENV = "CANONIC_PACKS_REPO"
_DEFAULT_PACKS_REPO = "https://github.com/mischuh/canonic-packs.git"


def _offer_context_packs(root: Path) -> tuple[Path, PackManifest] | None:
    """§5.1: list packs from the configured repo, or None on unreachable/none/declined.

    A network failure never blocks setup: printed once, then the caller falls through to
    the unchanged generic path.
    """
    from canonic.packs.loader import list_packs
    from canonic.packs.repo import resolve_repo

    repo_value = os.environ.get(_DEFAULT_PACKS_REPO_ENV) or _DEFAULT_PACKS_REPO
    try:
        repo_dir = resolve_repo(repo_value, project_root=root)
        manifests = list_packs(repo_dir)
    except CanonicError as exc:
        _console.print(f"[yellow]pack repo unreachable, continuing without packs:[/yellow] {exc}")
        logger.info("setup: pack repo unreachable: %s", exc)
        return None
    if not manifests:
        return None

    _console.print(
        Panel.fit("Set up a known system, or infer from your database.", title="context packs")
    )
    for i, m in enumerate(manifests, 1):
        _console.print(f"  [{i}] {m.pack} — {m.description}")
    other_idx = len(manifests) + 1
    _console.print(f"  [{other_idx}] Something else (infer from my database)")
    choice = typer.prompt("Select", default=str(other_idx))
    try:
        idx = int(choice)
    except ValueError:
        idx = other_idx
    if not (1 <= idx <= len(manifests)):
        return None
    return repo_dir, manifests[idx - 1]


def _run_pack_branch(root: Path, project_name: str) -> bool:
    """§5.1 entry point for the fresh-project wizard.

    Returns True once canonic.yaml has been written for a chosen pack's connection — the
    caller returns immediately, even if the user then declines the pack's own file write
    (canonic.yaml already exists at that point, so falling through to the generic path
    would re-prompt for a connection and overwrite it). False means "continue the generic
    wizard path unchanged": no packs available/reachable, or the user picked "something
    else" — nothing has been written yet in either case.
    """
    offer = _offer_context_packs(root)
    if offer is None:
        return False
    repo_dir, manifest = offer
    return _run_pack_setup(root, project_name, repo_dir, manifest)


def _prompt_pack_variant(manifest: PackManifest) -> Variant:
    _console.print("multiple variants available:")
    for i, v in enumerate(manifest.variants, 1):
        _console.print(f"  [{i}] {v.id} — {v.label}")
    choice = typer.prompt("Select variant", default="1")
    try:
        return manifest.variants[int(choice) - 1]
    except (ValueError, IndexError):
        return manifest.variants[0]


def _prompt_pack_connection(root: Path, variant: Variant) -> Connection:
    """§5.2: run the connection flow inline, pinned to the variant's connector type.

    Each variant declares the connector type it needs, so the chosen id is bound to
    ``connection_id`` — it is never asked as an ordinary param. Falls back to the
    generic type-picker menu (:func:`_prompt_connection`) when a variant declares no
    connector (not used by the shipped PostHog pack, but the manifest allows it).
    """
    prompt_fn = _CONNECTOR_PROMPTS.get(variant.connector) if variant.connector else None
    if prompt_fn is None:
        return _prompt_connection(root)
    _console.print(
        Panel.fit(
            f"Configure the {variant.connector} connection this pack needs.", title="connection"
        )
    )
    while True:
        conn = prompt_fn()
        health = _test_connection(conn)
        if health is not None and health.status == "ok":
            _console.print("[green]✓[/green] connection test passed")
            _print_health_warnings(health)
            return conn
        if health is not None:
            _console.print(f"[red]connection test failed:[/red] {health.message}")
        if not typer.confirm("Try again?", default=True):
            raise typer.Exit(1)


def _location_check_with_fallback(
    manifest: PackManifest, seeded: dict[str, str], connection: Connection
) -> dict[str, str]:
    """§5.3: on a required_tables miss, offer schemas that contain a same-named table.

    Only offered when the manifest declares a ``schema`` param (the amendment's own
    worked example names it exactly that, §2.2) — nothing to rebind otherwise, so the
    precise §2.4 error is raised as-is.
    """
    from canonic.packs.install import check_required_tables
    from canonic.packs.templating import substitute

    try:
        check_required_tables(manifest, seeded, connection)
        return seeded
    except PackError as exc:
        original = exc

    if manifest.param("schema") is None:
        raise original

    relations = discover_relations(connection) or []
    expected = {
        substitute(t, seeded, source="pack.yaml#required_tables").rsplit(".", 1)[-1]
        for t in manifest.required_tables
    }
    candidates = sorted(
        {
            r.relation.rsplit(".", 1)[0]
            for r in relations
            if "." in r.relation and r.relation.rsplit(".", 1)[-1] in expected
        }
    )
    if not candidates:
        raise original

    _console.print(f"[yellow]{original}[/yellow]")
    _console.print("found matching table name(s) in these schemas:")
    for i, s in enumerate(candidates, 1):
        _console.print(f"  [{i}] {s}")
    choice = typer.prompt("Select schema", default="1")
    try:
        picked = candidates[int(choice) - 1]
    except (ValueError, IndexError):
        raise original from None

    seeded = {**seeded, "schema": picked}
    check_required_tables(manifest, seeded, connection)  # raises the precise error if still broken
    return seeded


def _resolve_pack_install_params(
    root: Path, manifest: PackManifest, connection: Connection
) -> dict[str, str]:
    """§5.4/§5.5 step 4: plain questions → location check (§5.3) → choose_from → derive."""
    from canonic.cli.commands._pack_prompts import prompt_choose_from, prompt_plain, seed_defaults
    from canonic.packs.install import resolve_params

    explicit: dict[str, str] = {"connection_id": connection.id}
    for p in manifest.params:
        if p.name in explicit or p.derive is not None or p.choose_from is not None:
            continue
        explicit[p.name] = prompt_plain(p)

    seeded = seed_defaults(manifest, explicit)
    seeded = _location_check_with_fallback(manifest, seeded, connection)
    if "schema" in seeded:
        explicit["schema"] = seeded["schema"]

    cache: dict[str, list[tuple[str, int]]] = {}
    for p in manifest.params:
        if p.name in explicit or p.choose_from is None:
            continue
        explicit[p.name] = prompt_choose_from(root, p, manifest, seeded, connection.id, cache)

    return resolve_params(manifest, explicit)


def _render_pack_file_preview(manifest: PackManifest, variant: Variant) -> None:
    """§5.5 step 5: the list of files to be written. Nothing is written before this."""
    _console.print(f"\n[bold]{manifest.pack} / {variant.id}[/bold] — files to be written:")
    for rel in (
        *manifest.provides.semantics,
        *manifest.provides.contracts.metrics,
        *manifest.provides.contracts.guardrails,
        *manifest.provides.knowledge,
    ):
        _console.print(f"  {rel}")


def _run_pack_setup(root: Path, project_name: str, repo_dir: Path, manifest: PackManifest) -> bool:
    """§5.2-§5.7 for a fresh project: connection → params → preview/confirm → write."""
    from canonic.cli.commands._pack_prompts import (
        render_install_result,
        run_and_render_first_answer,
    )
    from canonic.packs.install import install_pack
    from canonic.packs.loader import find_pack_dir

    manifest.check_compatible()
    variant = (
        manifest.variants[0] if len(manifest.variants) == 1 else _prompt_pack_variant(manifest)
    )
    manifest = manifest.for_variant(variant.id)
    pack_dir = find_pack_dir(repo_dir, manifest.pack)
    connection = _prompt_pack_connection(root, variant)

    # canonic.yaml is written now, right after the connection is bound — mirrors the
    # generic wizard (STEP_CONNECTION's choice is committed before anything downstream
    # runs) and is required here: choose_from (§5.4.1) runs a real query through
    # CanonicService.from_project(root), which needs a real canonic.yaml to load.
    config = CanonicConfig(
        version=1,
        project=ProjectConfig(name=project_name, default_connection=connection.id),
        connections=[connection],
        telemetry=TelemetryConfig(),
    )
    created = scaffold_project(root)
    dump_config(config, root / "canonic.yaml")
    load_config(root / "canonic.yaml")  # assert the written file round-trips
    clear_state(root)

    params = _resolve_pack_install_params(root, manifest, connection)

    _render_pack_file_preview(manifest, variant)
    if not typer.confirm("Write these files?", default=True):
        # canonic.yaml already exists at this point — falling through to the generic
        # wizard path would re-prompt for a connection and overwrite it, so this always
        # ends the wizard here, just without the pack's own files.
        _console.print(
            "[yellow]aborted:[/yellow] pack files not written; the connection is kept. "
            "Run `canonic setup` again to install a pack, or edit "
            "semantics/contracts/knowledge by hand."
        )
        _render_setup_complete(config, created, demo_ok=False, withheld_count=0)
        return True

    result = install_pack(root, pack_dir, manifest, variant, params)
    render_install_result(result)

    demo_ok = False
    if manifest.first_answer is not None:
        demo_ok = run_and_render_first_answer(root, manifest.first_answer)

    logger.info("setup: pack %s installed, first_answer_ok=%s", manifest.pack, demo_ok)
    _render_setup_complete(config, created, demo_ok=demo_ok, withheld_count=0)
    return True


def _add_pack_to_existing(root: Path) -> None:
    """Install a context pack into an already-configured project (existing-project menu)."""
    from canonic.cli.commands._pack_prompts import (
        render_install_result,
        run_and_render_first_answer,
    )
    from canonic.packs.install import install_pack
    from canonic.packs.loader import find_pack_dir

    offer = _offer_context_packs(root)
    if offer is None:
        _console.print("[dim]no packs available.[/dim]")
        return
    repo_dir, manifest = offer
    manifest.check_compatible()

    config = load_config(root / "canonic.yaml")
    variant = (
        manifest.variants[0] if len(manifest.variants) == 1 else _prompt_pack_variant(manifest)
    )
    manifest = manifest.for_variant(variant.id)
    pack_dir = find_pack_dir(repo_dir, manifest.pack)

    candidates = [
        c for c in config.connections if variant.connector is None or c.type == variant.connector
    ]
    if not candidates:
        connection = _prompt_pack_connection(root, variant)
        # Persisted immediately (not deferred to the write-preview confirm below):
        # choose_from (§5.4.1) needs this connection loadable from canonic.yaml before
        # its query can run through CanonicService.from_project(root).
        path = root / "canonic.yaml"
        raw = load_raw_config(path)
        raw.setdefault("connections", [])
        raw["connections"].append(connection.model_dump(mode="json", exclude_none=True))
        write_raw_config(path, raw)
        logger.info(
            "setup: connection %s (%s) added for pack %s",
            connection.id,
            connection.type,
            manifest.pack,
        )
    elif len(candidates) == 1:
        connection = candidates[0]
    else:
        _console.print(f"multiple {variant.connector} connections found:")
        for i, c in enumerate(candidates, 1):
            _console.print(f"  [{i}] {c.id}")
        choice = typer.prompt("Select connection", default="1")
        try:
            connection = candidates[int(choice) - 1]
        except (ValueError, IndexError):
            connection = candidates[0]

    params = _resolve_pack_install_params(root, manifest, connection)

    _render_pack_file_preview(manifest, variant)
    if not typer.confirm("Write these files?", default=True):
        _console.print("[yellow]aborted:[/yellow] nothing written")
        return

    result = install_pack(root, pack_dir, manifest, variant, params)
    render_install_result(result)
    if manifest.first_answer is not None:
        run_and_render_first_answer(root, manifest.first_answer)


# --- shared prompts --------------------------------------------------------


def _prompt_connection_or_skip(root: Path) -> Connection | None:
    """Offer to configure a data connection now, or skip it.

    Declining leaves ``connections: []`` in canonic.yaml; the golden path degrades to a
    scaffold-only completion. A connection can be added later via the existing-project
    menu's "add connection" option (``_add_connection_to_existing``) or by editing
    canonic.yaml directly.
    """
    if not typer.confirm("Configure a data connection now?", default=True):
        _console.print(
            "[dim]skipping connection — add one later via `canonic setup` "
            "or by editing canonic.yaml.[/dim]"
        )
        return None
    return _prompt_connection(root)


def _prompt_connection(root: Path) -> Connection:
    """Prompt for connection type then collect type-specific params, test-gated."""
    _console.print(Panel.fit("Configure the first data connection.", title="connection"))
    width = max(len(c.label) for c in _CONNECTOR_CHOICES)
    for number, spec in enumerate(_CONNECTOR_CHOICES, start=1):
        _console.print(f"  [bold][{number}][/bold] {spec.label:<{width}} {spec.description}")
    numbers = " / ".join(f"{n}={c.type}" for n, c in enumerate(_CONNECTOR_CHOICES, start=1))
    valid = ", ".join(str(n) for n in range(1, len(_CONNECTOR_CHOICES) + 1))
    while True:
        choice = typer.prompt(f"Type [{numbers}]", default="1")
        if not (choice.isdigit() and 1 <= int(choice) <= len(_CONNECTOR_CHOICES)):
            _console.print(f"[red]enter one of {valid}[/red]")
            continue
        spec = _CONNECTOR_CHOICES[int(choice) - 1]
        conn = spec.prompt()

        health = _test_connection(conn)
        if health is not None and health.status == "ok":
            _console.print("[green]✓[/green] connection test passed")
            _print_health_warnings(health)
            if spec.narrows_schema:
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


def _prompt_snowflake_params() -> Connection:
    """Collect params for a Snowflake connection (account + password or key pair).

    With key-pair auth ``credentials_ref`` only holds the optional key passphrase.
    """
    conn_id = typer.prompt("Connection id", default="warehouse_sf")
    params: dict[str, object] = {
        "account": typer.prompt("Account identifier (e.g. xy12345.eu-central-1)"),
        "user": typer.prompt("User"),
        "warehouse": typer.prompt("Warehouse"),
        "database": typer.prompt("Database"),
    }
    role = typer.prompt(
        "Read-only role holding SELECT grants only (recommended, empty uses the user's default)",
        default="",
    )
    while (auth := typer.prompt("Authentication [1=password / 2=key pair]", default="1")) not in (
        "1",
        "2",
    ):
        _console.print("[red]enter 1 or 2[/red]")
    credentials_ref: str | None
    if auth == "2":
        key_path = typer.prompt("Path to the PEM private key")
        if not Path(key_path).expanduser().is_file():
            _console.print(f"[yellow]note:[/yellow] no file found at {key_path}")
        params["private_key_path"] = key_path
        env_var = typer.prompt(
            "Env var holding the key passphrase (leave empty for an unencrypted key)",
            default="",
        )
        credentials_ref = f"env:{env_var}" if env_var else None
        secret_label = "passphrase"
    else:
        env_var = typer.prompt(
            "Env var holding the password",
            default=f"CANONIC_{conn_id.upper()}_PASSWORD",
        )
        credentials_ref = f"env:{env_var}"
        secret_label = "password"
    if env_var and not os.environ.get(env_var):
        _console.print(
            f"\n[yellow]note:[/yellow] [bold]{env_var}[/bold] is not set in your current shell.\n"
            f"  Before the connection test runs, open a new terminal tab and export it:\n"
            f"  [bold]export {env_var}=<your-{secret_label}>[/bold]\n"
            "  Setup progress is saved. If you need to exit now, re-run [bold]canonic setup[/bold] and it will resume here.\n"
        )
    return Connection(
        id=conn_id,
        type="snowflake",
        params=params,
        credentials_ref=credentials_ref,
        read_only_role=role or None,
    )


def _prompt_databricks_params() -> Connection:
    """Collect params for a Databricks SQL warehouse connection (host, HTTP path, token)."""
    conn_id = typer.prompt("Connection id", default="warehouse_dbx")
    params: dict[str, object] = {
        "server_hostname": typer.prompt("Server hostname (e.g. dbc-1234.cloud.databricks.com)"),
        "http_path": typer.prompt("SQL warehouse HTTP path (e.g. /sql/1.0/warehouses/abc123)"),
        "catalog": typer.prompt("Catalog"),
    }
    env_var = typer.prompt(
        "Env var holding the access token",
        default=f"CANONIC_{conn_id.upper()}_TOKEN",
    )
    if not os.environ.get(env_var):
        _console.print(
            f"\n[yellow]note:[/yellow] [bold]{env_var}[/bold] is not set in your current shell.\n"
            f"  Before the connection test runs, open a new terminal tab and export it:\n"
            f"  [bold]export {env_var}=<your-access-token>[/bold]\n"
            "  Setup progress is saved. If you need to exit now, re-run [bold]canonic setup[/bold] and it will resume here.\n"
        )
    return Connection(
        id=conn_id,
        type="databricks",
        params=params,
        credentials_ref=f"env:{env_var}",
    )


def _prompt_mysql_params() -> Connection:
    """Collect params for a MySQL connection (server + credentials env var)."""
    conn_id = typer.prompt("Connection id", default="warehouse_mysql")
    params: dict[str, object] = {
        "host": typer.prompt("Host", default="localhost"),
        "port": typer.prompt("Port", default=3306, type=int),
        "user": typer.prompt("User"),
    }
    database = typer.prompt(
        "Database (leave empty for every database the user can see)", default=""
    )
    if database:
        params["database"] = database
    env_var = typer.prompt(
        "Env var holding the password",
        default=f"CANONIC_{conn_id.upper()}_PASSWORD",
    )
    if not os.environ.get(env_var):
        _console.print(
            f"\n[yellow]note:[/yellow] [bold]{env_var}[/bold] is not set in your current shell.\n"
            f"  Before the connection test runs, open a new terminal tab and export it:\n"
            f"  [bold]export {env_var}=<your-password>[/bold]\n"
            "  Setup progress is saved. If you need to exit now, re-run [bold]canonic setup[/bold] and it will resume here.\n"
        )
    return Connection(
        id=conn_id,
        type="mysql",
        params=params,
        credentials_ref=f"env:{env_var}",
    )


def _prompt_clickhouse_params() -> Connection:
    """Collect params for a ClickHouse connection (HTTP(S) endpoint + credentials env var)."""
    conn_id = typer.prompt("Connection id", default="warehouse_clickhouse")
    host = typer.prompt("Host", default="localhost")
    secure = typer.confirm("Connect over HTTPS (needed for ClickHouse Cloud)?", default=False)
    params: dict[str, object] = {
        "host": host,
        "port": typer.prompt("HTTP port", default=8443 if secure else 8123, type=int),
        "user": typer.prompt("User", default="default"),
    }
    if secure:
        params["secure"] = True
    database = typer.prompt("Database (leave empty for the user's default database)", default="")
    if database:
        params["database"] = database
    env_var = typer.prompt(
        "Env var holding the password",
        default=f"CANONIC_{conn_id.upper()}_PASSWORD",
    )
    if not os.environ.get(env_var):
        _console.print(
            f"\n[yellow]note:[/yellow] [bold]{env_var}[/bold] is not set in your current shell.\n"
            f"  Before the connection test runs, open a new terminal tab and export it:\n"
            f"  [bold]export {env_var}=<your-password>[/bold]\n"
            "  Setup progress is saved. If you need to exit now, re-run [bold]canonic setup[/bold] and it will resume here.\n"
        )
    return Connection(
        id=conn_id,
        type="clickhouse",
        params=params,
        credentials_ref=f"env:{env_var}",
    )


@dataclasses.dataclass(frozen=True, slots=True)
class _ConnectorChoice:
    """One entry of the connection type menu: how it is listed and how its params are asked."""

    type: str
    label: str
    description: str
    prompt: Callable[[], Connection]
    #: Whether a passing connection test offers to narrow introspection to some schemas.
    narrows_schema: bool = False


#: The connection type menu in display order. The position is the number the user types.
#: A new warehouse is added here and in the connector factory, nowhere else.
_CONNECTOR_CHOICES: tuple[_ConnectorChoice, ...] = (
    _ConnectorChoice(
        "sqlite",
        "SQLite",
        "— local .db file, no credentials, works offline [dim](recommended for a first try)[/dim]",
        _prompt_sqlite_params,
    ),
    _ConnectorChoice(
        "duckdb",
        "DuckDB",
        "— local .duckdb file, analytical workloads, no credentials",
        _prompt_duckdb_params,
    ),
    _ConnectorChoice(
        "postgres",
        "Postgres",
        "— server-based, requires host/port/credentials",
        _prompt_postgres_params,
        narrows_schema=True,
    ),
    _ConnectorChoice(
        "redshift",
        "Redshift",
        "— Amazon Redshift, requires host/port/credentials",
        _prompt_redshift_params,
        narrows_schema=True,
    ),
    _ConnectorChoice(
        "snowflake",
        "Snowflake",
        "— requires account/user/warehouse/credentials",
        _prompt_snowflake_params,
        narrows_schema=True,
    ),
    _ConnectorChoice(
        "databricks",
        "Databricks",
        "— SQL warehouse, requires hostname/HTTP path/access token",
        _prompt_databricks_params,
        narrows_schema=True,
    ),
    _ConnectorChoice(
        "mysql",
        "MySQL",
        "— server-based, MySQL 8.0 or newer, requires host/port/credentials",
        _prompt_mysql_params,
        narrows_schema=True,
    ),
    _ConnectorChoice(
        "clickhouse",
        "ClickHouse",
        "— server-based or ClickHouse Cloud, requires host/port/credentials",
        _prompt_clickhouse_params,
        narrows_schema=True,
    ),
)

#: connector type → the prompt function collecting its params, for a pack variant's
#: pinned ``connector`` (§5.2, `_prompt_pack_connection` above).
_CONNECTOR_PROMPTS: dict[str, Callable[[], Connection]] = {
    c.type: c.prompt for c in _CONNECTOR_CHOICES
}


def _print_health_warnings(health: Health) -> None:
    """Show the non-fatal findings of a passing connection test."""
    for warning in health.warnings:
        _console.print(f"[yellow]warning:[/yellow] {warning}")


def _test_connection(conn: Connection) -> Health | None:
    """Run the async connection test; return None when the test could not run."""
    logger.info("setup: testing connection id=%s type=%s", conn.id, conn.type)
    try:
        health = asyncio.run(_probe(conn))
        logger.info("setup: connection test result: id=%s status=%s", conn.id, health.status)
        return health
    except CredentialError as exc:
        logger.warning("setup: connection test credential error: id=%s: %s", conn.id, exc)
        _console.print(f"[red]credential error:[/red] {exc}")
        if conn.credentials_ref and conn.credentials_ref.startswith("env:"):
            env_var = conn.credentials_ref[4:]
            secret_label = "passphrase" if conn.params.get("private_key_path") else "password"
            _console.print(
                f"  Set it now:  [bold]export {env_var}=<your-{secret_label}>[/bold]\n"
                "  Progress is saved — Ctrl-C, set the var, then re-run [bold]canonic setup[/bold] to resume."
            )
        return None
    except ConnectionError as exc:
        logger.warning("setup: cannot build connector: id=%s: %s", conn.id, exc)
        _console.print(f"[red]cannot build connector:[/red] {exc}")
        return None


async def _probe(conn: Connection) -> Health:
    connector = default_factory.create(conn)
    try:
        return await connector.test_connection()
    finally:
        await connector.aclose()


def _prompt_llm_or_skip() -> LLMConfig | None:
    """Offer to configure an LLM now, or skip it.

    The deterministic core and demo query need no model; declining leaves ``llm: null``.
    An LLM can be added later via the existing-project menu's "configure LLM" option
    (``_add_llm_to_existing``) or by editing canonic.yaml directly.
    """
    if not typer.confirm("Configure an LLM now?", default=True):
        _console.print(
            "[dim]skipping LLM — the deterministic core and first answer don't need one.[/dim]"
        )
        return None
    return _prompt_llm()


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
        relations = asyncio.run(introspect_connection(conn))
    except (CredentialError, ConnectionError) as exc:
        _console.print(f"[yellow]schema preview skipped:[/yellow] {exc}")
        return False
    _console.print(f"[green]✓[/green] found {len(relations)} relations")
    return True


def _maybe_narrow_schema(conn: Connection) -> Connection:
    """Offer to discover schemas/tables and narrow conn.params to the user's picks."""
    if not typer.confirm("Narrow down schemas/tables now?", default=True):
        return conn
    relations = discover_relations(conn)
    if relations is None:
        return conn
    if not relations:
        _console.print("[dim]no relations found — nothing to narrow.[/dim]")
        return conn

    schemas = sorted({r.relation.split(".", 1)[0] for r in relations})
    selected_schemas = prompt_select_schemas(schemas)
    if selected_schemas is not None:
        conn.params["schemas"] = selected_schemas
        relations = [r for r in relations if r.relation.split(".", 1)[0] in selected_schemas]

    selected_tables = prompt_select_tables(relations)
    if selected_tables is not None:
        conn.params["tables"] = selected_tables

    return conn
