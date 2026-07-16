"""``canonic ingest`` — run the four-stage ingestion pipeline (SPEC-E4 §2, §7, §8, §9).

Refreshes context from the configured sources: introspects each connection into normalized
evidence, drafts proposals, reconciles them against the accepted files, validates the proposed
state, and emits reviewable diffs plus a ``ReconciliationReport``. Propose-only by default
(§5.5) — it writes the audit trail and refreshes ``last_validated_at`` on unchanged files but
edits no committed semantics in place. ``--bootstrap`` is the fast initial path for a fresh
connection (§8); ``--dry-run`` computes and prints diffs while touching nothing.

Headless mode (§9): ``--headless`` (or an auto-detected ``CI=true``) pins the deterministic
builder, opens an auto-PR carrying the diffs and contradiction notes, and gates the run on the
canonical exit codes — the safe, repeatable scheduled-ingest role (PRD §5.6).
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import TYPE_CHECKING, Annotated

import typer

from canonic.cli._errors import get_cli_context, handle_errors
from canonic.cli.commands import _console, load_raw_config, write_raw_config
from canonic.cli.commands._schema_selection import (
    discover_relations,
    prompt_select_schemas,
    prompt_select_tables,
)
from canonic.config import find_project_root, load_config
from canonic.connectors.evidence import GenericEvidenceConnector, NullExtractionSkill
from canonic.connectors.factory import default_factory
from canonic.exc import CanonicError, ConnectionError, ContradictionsFound
from canonic.feedback.evidence import outcome_evidence
from canonic.feedback.history import BindingOutcomeHistory
from canonic.ingestion.autopr import AutoPRPublisher, PullRequestPublisher, SubprocessPublisher
from canonic.ingestion.models import ReconciliationDecision
from canonic.ingestion.pipeline import IngestionPipeline
from canonic.ingestion.source import gather_evidence
from canonic.instrumentation.events import emit_milestone_once
from canonic.instrumentation.models import FunnelMilestone
from canonic.runtime.drafter import make_drafter, make_reconcile_drafter
from canonic.runtime.extraction import make_extraction_skill
from canonic.semantic.loader import list_semantic_sources

if TYPE_CHECKING:
    from collections.abc import Awaitable
    from pathlib import Path

    from canonic.config import CanonicConfig, Connection
    from canonic.connectors.base import ConnectorBase
    from canonic.ingestion.models import EvidenceItem
    from canonic.ingestion.pipeline import PipelineResult

logger = logging.getLogger(__name__)

app = typer.Typer(
    name="ingest",
    help="Reconcile configured sources into reviewable context diffs.",
    invoke_without_command=True,
)


@app.callback(invoke_without_command=True)
@handle_errors
def ingest(
    ctx: typer.Context,
    bootstrap: Annotated[
        bool,
        typer.Option(
            "--bootstrap", help="Fast initial bootstrap: introspect and draft one connection."
        ),
    ] = False,
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Print proposed diffs and write nothing."),
    ] = False,
    connection: Annotated[
        str | None,
        typer.Option("--connection", help="Limit the run to a single connection id."),
    ] = None,
    headless: Annotated[
        bool,
        typer.Option("--headless", help="Force headless mode (also auto-detected via CI=true)."),
    ] = False,
    strict: Annotated[
        bool,
        typer.Option("--strict", help="Fail the run (exit 14) if any contradiction is flagged."),
    ] = False,
    open_pr: Annotated[
        bool | None,
        typer.Option(
            "--open-pr/--no-pr",
            help="Force/suppress the auto-PR (defaults to on in headless mode).",
        ),
    ] = None,
) -> None:
    """Reconcile configured sources into reviewable context diffs."""
    if ctx.invoked_subcommand is not None:
        return

    root = find_project_root()
    if root is None:
        _console.print(
            "[red]error:[/red] no canonic project found — run from inside a project directory"
        )
        raise typer.Exit(1)

    config = load_config(root / "canonic.yaml")
    targets = _select_connections(config, bootstrap=bootstrap, connection=connection)
    _run_and_report(
        ctx,
        root,
        config,
        targets,
        bootstrap=bootstrap,
        dry_run=dry_run,
        headless=headless,
        strict=strict,
        open_pr=open_pr,
    )


def _run_and_report(
    ctx: typer.Context,
    root: Path,
    config: CanonicConfig,
    targets: list[Connection],
    *,
    bootstrap: bool,
    dry_run: bool,
    headless: bool,
    strict: bool,
    open_pr: bool | None,
) -> PipelineResult:
    """Run the pipeline for ``targets`` and render/gate on the result.

    Shared by the default ``canonic ingest`` run and ``canonic ingest add-tables`` so both
    go through identical output rendering, milestone emission, auto-PR, and the strict gate.
    """
    is_headless = _is_headless(headless)
    logger.info(
        "ingest: connections=%s bootstrap=%s dry_run=%s headless=%s",
        [conn.id for conn in targets],
        bootstrap,
        dry_run,
        is_headless,
    )
    result = asyncio.run(
        _ingest(root, config, targets, bootstrap=bootstrap, dry_run=dry_run, headless=is_headless)
    )

    if get_cli_context(ctx).json_output:
        typer.echo(result.emission.to_json())
    else:
        typer.echo(result.emission.render_markdown())

    if bootstrap and not result.first_run and not get_cli_context(ctx).json_output:
        _console.print(
            "[yellow]note:[/yellow] accepted context already exists — "
            "auto-accept skipped; run behaved as normal propose-only ingest (OB-S3)"
        )

    if not dry_run:
        emit_milestone_once(root, FunnelMilestone.FIRST_CURATED_REVIEW_COMPLETED)

    # Auto-PR before the strict gate so the PR still carries the contradiction notes, then CI
    # fails the run on the gate (SPEC-E4 §6/§5.4).
    if _should_open_pr(open_pr, headless=is_headless) and not dry_run and not bootstrap:
        logger.info("opening auto-PR")
        pr_ref = asyncio.run(_open_auto_pr(root, result))
        if pr_ref:
            logger.info("auto-PR opened: %s", pr_ref)
            if not get_cli_context(ctx).json_output:
                typer.echo(f"opened auto-PR: {pr_ref}")

    if strict or config.reconcile.strict_contradictions:
        contradictions = result.report.summary[ReconciliationDecision.CONTRADICTION.value]
        if contradictions > 0:
            logger.info("strict mode: gating run on %d contradiction(s)", contradictions)
            raise ContradictionsFound(
                f"{contradictions} contradiction(s) flagged; strict mode gates the run (E4 §5.4)"
            )

    return result


@app.command("add-tables")
@handle_errors
def add_tables(
    ctx: typer.Context,
    connection: Annotated[
        str | None,
        typer.Option(
            "--connection", "-c", help="Connection id to widen (default: the only one configured)."
        ),
    ] = None,
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Print proposed diffs and write nothing."),
    ] = False,
) -> None:
    """Pick new tables from a connection and propose them for the semantic model.

    Reuses the schema/table picker from ``canonic setup``, excluding tables already
    curated under ``semantics/`` for this connection, then runs a normal ingest scoped
    to what was picked — curated tables are never touched (SPEC-E4 §5.5).
    """
    root = find_project_root()
    if root is None:
        _console.print(
            "[red]error:[/red] no canonic project found — run from inside a project directory"
        )
        raise typer.Exit(1)

    config = load_config(root / "canonic.yaml")
    conn = _resolve_one_connection(config, connection)

    imported = {
        source.table for source in list_semantic_sources(root) if source.connection == conn.id
    }

    unfiltered = conn.model_copy(
        update={"params": {k: v for k, v in conn.params.items() if k not in ("schemas", "tables")}}
    )
    relations = discover_relations(unfiltered)
    if relations is None:
        raise typer.Exit(1)

    candidates = [r for r in relations if r.relation not in imported]
    skipped = len(relations) - len(candidates)
    if skipped:
        _console.print(
            f"[dim]{skipped} table(s) already in the semantic model — excluded from this list.[/dim]"
        )
    if not candidates:
        _console.print("[green]nothing to add[/green] — every discovered table is already curated.")
        return

    schemas = sorted({r.relation.split(".", 1)[0] for r in candidates})
    selected_schemas = prompt_select_schemas(schemas)
    if selected_schemas is not None:
        candidates = [r for r in candidates if r.relation.split(".", 1)[0] in selected_schemas]
        if not candidates:
            _console.print(
                "[green]nothing to add[/green] — no not-yet-curated tables in the selected schema(s)."
            )
            return

    selected_tables = prompt_select_tables(candidates)
    chosen = selected_tables if selected_tables is not None else [r.relation for r in candidates]

    existing_filter = conn.params.get("tables")
    if existing_filter is not None:
        merged = sorted(set(existing_filter) | set(chosen))
        _persist_tables_filter(root / "canonic.yaml", conn.id, merged)
        conn.params["tables"] = merged
        run_conn = conn
        _console.print(
            f"[green]✓[/green] widened connection [bold]{conn.id}[/bold] tables filter "
            f"to {len(merged)} pattern(s) in canonic.yaml"
        )
    else:
        # Connection was already unfiltered (all tables visible); scope only this run to
        # what was picked instead of persisting a filter that would narrow future ingests.
        run_conn = conn.model_copy(update={"params": {**conn.params, "tables": chosen}})

    _run_and_report(
        ctx,
        root,
        config,
        [run_conn],
        bootstrap=False,
        dry_run=dry_run,
        headless=False,
        strict=False,
        open_pr=False,
    )


def _resolve_one_connection(config: CanonicConfig, connection: str | None) -> Connection:
    """Resolve the single connection ``add-tables`` should widen (SPEC-E4 §7 style)."""
    by_id = {conn.id: conn for conn in config.connections}
    if connection is not None:
        if connection not in by_id:
            known = ", ".join(by_id) or "(none)"
            raise ConnectionError(f"unknown connection {connection!r}; configured: {known}")
        return by_id[connection]
    if not config.connections:
        raise ConnectionError("project has no configured connections")
    if len(config.connections) > 1:
        known = ", ".join(by_id)
        raise ConnectionError(
            f"multiple connections configured ({known}); pass --connection to pick one"
        )
    return config.connections[0]


def _persist_tables_filter(config_path: Path, connection_id: str, tables: list[str]) -> None:
    """Merge a widened ``tables`` filter into ``connection_id``'s entry in canonic.yaml."""
    raw = load_raw_config(config_path)
    for entry in raw.get("connections", []):
        if entry.get("id") == connection_id:
            entry.setdefault("params", {})
            entry["params"]["tables"] = tables
            break
    write_raw_config(config_path, raw)


def _is_headless(flag: bool) -> bool:
    """Headless if the flag is set or the run is in CI (SPEC-E4 §9)."""
    return flag or os.environ.get("CI") == "true"


def _should_open_pr(override: bool | None, *, headless: bool) -> bool:
    """Resolve the auto-PR decision: explicit ``--open-pr/--no-pr`` wins, else default to headless."""
    return override if override is not None else headless


def build_publisher(project_root: Path) -> PullRequestPublisher:
    """Construct the git/gh publisher for the auto-PR step (seam for test injection)."""
    return SubprocessPublisher(project_root)


async def _open_auto_pr(root: Path, result: PipelineResult) -> str | None:
    """Open the headless auto-PR for ``result`` and return its reference (SPEC-E4 §6)."""
    return await AutoPRPublisher(root, build_publisher(root)).publish(result)


def _select_connections(
    config: CanonicConfig, *, bootstrap: bool, connection: str | None
) -> list[Connection]:
    """Resolve which connections this run covers (SPEC-E4 §7 full ingest, §8 bootstrap).

    ``--connection`` scopes to one; bootstrap defaults to the first/default connection (its fast
    initial path is single-connection); a full ingest covers every configured connection.
    """
    by_id = {conn.id: conn for conn in config.connections}
    if connection is not None:
        if connection not in by_id:
            known = ", ".join(by_id) or "(none)"
            raise ConnectionError(f"unknown connection {connection!r}; configured: {known}")
        return [by_id[connection]]
    if not config.connections:
        raise ConnectionError("project has no configured connections to ingest")
    if bootstrap:
        first = config.project.default_connection or config.connections[0].id
        return [by_id.get(first, config.connections[0])]
    return list(config.connections)


async def _ingest(
    root: Path,
    config: CanonicConfig,
    targets: list[Connection],
    *,
    bootstrap: bool,
    dry_run: bool,
    headless: bool,
) -> PipelineResult:
    """Build connectors, gather evidence, and drive the pipeline; always closes connectors."""
    logger.debug("creating connector(s) for: %s", [conn.id for conn in targets])
    connectors: dict[str, ConnectorBase] = {
        conn.id: default_factory.create(conn) for conn in targets
    }

    # Bootstrap only introspects the default connection, but definition connectors (e.g.
    # a dbt manifest) need no live connection and provide business-named measures from
    # semantic models.  Build them alongside so pipeline.bootstrap can include their
    # evidence and avoid validation failures when metric contracts already exist.
    if bootstrap:
        from canonic.connectors.base import Capability

        target_ids = {conn.id for conn in targets}
        for conn in config.connections:
            if conn.id not in target_ids:
                extra = default_factory.create(conn)
                if Capability.EXTRACT_DEFINITIONS in extra.capabilities():
                    connectors[conn.id] = extra

    _wire_extraction_skills(connectors, config, headless=headless)

    drafter = make_drafter(config.llm, config.runtime, headless=headless)
    reconcile_drafter = make_reconcile_drafter(config.llm, config.runtime, headless=headless)
    pipeline = IngestionPipeline(
        root,
        connectors,
        config.reconcile,
        headless=headless,
        drafter=drafter,
        reconcile_drafter=reconcile_drafter,
    )
    try:
        if bootstrap:
            return await _guard_connection(targets[0].id, pipeline.bootstrap(targets[0].id))
        evidence = [
            item
            for conn in targets
            for item in await _gather_evidence(connectors[conn.id], conn.id)
        ]
        history = BindingOutcomeHistory.from_project(root)
        evidence += outcome_evidence(root, history, config.feedback)
        logger.info(
            "gathered %d evidence item(s) across %d connection(s)", len(evidence), len(targets)
        )
        return await pipeline.run(evidence, dry_run=dry_run)
    finally:
        logger.debug("closing %d connector(s)", len(connectors))
        for connector in connectors.values():
            await connector.aclose()


def _wire_extraction_skills(
    connectors: dict[str, ConnectorBase], config: CanonicConfig, *, headless: bool
) -> None:
    """Backfill a real ExtractionSkill into GenericEvidenceConnectors that default to Null.

    The connector factory only threads a bare ``Connection`` into connector builders
    (SPEC-E2 §2.2a) — it has no access to ``LLMConfig`` — so a ``GenericEvidenceConnector``
    without its own vendor-specific skill (e.g. Notion's deterministic
    ``NotionExtractionSkill``) defaults to ``NullExtractionSkill`` at factory-build time.
    Backfill the config-driven skill only for those; never override a connector's
    deliberate choice. Same seam as ``drafter``/``reconcile_drafter``, applied directly to
    the connector since ``extract_evidence()`` runs before any drafter exists.
    """
    extraction_skill = make_extraction_skill(config.llm, config.runtime, headless=headless)
    for connector in connectors.values():
        if isinstance(connector, GenericEvidenceConnector) and isinstance(
            connector.extraction_skill, NullExtractionSkill
        ):
            connector.set_extraction_skill(extraction_skill)


async def _gather_evidence(connector: ConnectorBase, conn_id: str) -> list[EvidenceItem]:
    """Gather evidence by dispatching on declared capabilities (SPEC-E3 §2, S4).

    Translates transport failures into ``CONNECTION_ERROR`` (exit 13).
    """
    try:
        return await gather_evidence(connector, conn_id)
    except CanonicError:
        raise
    except Exception as exc:  # noqa: BLE001 — any connector/transport failure ⇒ unreachable source
        raise ConnectionError(f"source {conn_id!r} unreachable: {exc}") from exc


async def _guard_connection(conn_id: str, coro: Awaitable[PipelineResult]) -> PipelineResult:
    """Await ``coro``, translating non-canonic (transport) failures into ``CONNECTION_ERROR``.

    Canonic errors (e.g. ``VALIDATION_FAILED`` from the gate) pass through untouched; only an
    unexpected connector/transport failure becomes a ``ConnectionError`` (exit 13).
    """
    try:
        return await coro
    except CanonicError:
        raise
    except Exception as exc:  # noqa: BLE001 — any connector/transport failure ⇒ unreachable source
        raise ConnectionError(f"source {conn_id!r} unreachable: {exc}") from exc
