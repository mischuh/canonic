"""``canon ingest`` — run the four-stage ingestion pipeline (SPEC-E4 §2, §7, §8, §9).

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
import os
from typing import TYPE_CHECKING, Annotated

import typer

from canon.cli._errors import get_cli_context, handle_errors
from canon.cli.commands import _console
from canon.config import find_project_root, load_config
from canon.connectors.factory import default_factory
from canon.exc import CanonError, ConnectionError, ContradictionsFound
from canon.ingestion.autopr import AutoPRPublisher, PullRequestPublisher, SubprocessPublisher
from canon.ingestion.models import ReconciliationDecision
from canon.ingestion.pipeline import IngestionPipeline
from canon.ingestion.source import gather_evidence
from canon.runtime.drafter import make_drafter, make_reconcile_drafter

if TYPE_CHECKING:
    from collections.abc import Awaitable
    from pathlib import Path

    from canon.config import CanonConfig, Connection
    from canon.connectors.base import ConnectorBase
    from canon.ingestion.models import EvidenceItem
    from canon.ingestion.pipeline import PipelineResult


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
    """Reconcile configured sources into reviewable context diffs (SPEC-E4 §2)."""
    root = find_project_root()
    if root is None:
        _console.print(
            "[red]error:[/red] no canon project found — run from inside a project directory"
        )
        raise typer.Exit(1)

    is_headless = _is_headless(headless)
    config = load_config(root / "canon.yaml")
    targets = _select_connections(config, bootstrap=bootstrap, connection=connection)
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

    # Auto-PR before the strict gate so the PR still carries the contradiction notes, then CI
    # fails the run on the gate (SPEC-E4 §6/§5.4).
    if _should_open_pr(open_pr, headless=is_headless) and not dry_run and not bootstrap:
        pr_ref = asyncio.run(_open_auto_pr(root, result))
        if pr_ref and not get_cli_context(ctx).json_output:
            typer.echo(f"opened auto-PR: {pr_ref}")

    if strict or config.reconcile.strict_contradictions:
        contradictions = result.report.summary[ReconciliationDecision.CONTRADICTION.value]
        if contradictions > 0:
            raise ContradictionsFound(
                f"{contradictions} contradiction(s) flagged; strict mode gates the run (E4 §5.4)"
            )


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
    config: CanonConfig, *, bootstrap: bool, connection: str | None
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
    config: CanonConfig,
    targets: list[Connection],
    *,
    bootstrap: bool,
    dry_run: bool,
    headless: bool,
) -> PipelineResult:
    """Build connectors, gather evidence, and drive the pipeline; always closes connectors."""
    connectors: dict[str, ConnectorBase] = {
        conn.id: default_factory.create(conn) for conn in targets
    }
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
        return await pipeline.run(evidence, dry_run=dry_run)
    finally:
        for connector in connectors.values():
            await connector.aclose()


async def _gather_evidence(connector: ConnectorBase, conn_id: str) -> list[EvidenceItem]:
    """Gather evidence by dispatching on declared capabilities (SPEC-E3 §2, S4).

    Translates transport failures into ``CONNECTION_ERROR`` (exit 13).
    """
    try:
        return await gather_evidence(connector, conn_id)
    except CanonError:
        raise
    except Exception as exc:  # noqa: BLE001 — any connector/transport failure ⇒ unreachable source
        raise ConnectionError(f"source {conn_id!r} unreachable: {exc}") from exc


async def _guard_connection(conn_id: str, coro: Awaitable[PipelineResult]) -> PipelineResult:
    """Await ``coro``, translating non-canon (transport) failures into ``CONNECTION_ERROR``.

    Canon errors (e.g. ``VALIDATION_FAILED`` from the gate) pass through untouched; only an
    unexpected connector/transport failure becomes a ``ConnectionError`` (exit 13).
    """
    try:
        return await coro
    except CanonError:
        raise
    except Exception as exc:  # noqa: BLE001 — any connector/transport failure ⇒ unreachable source
        raise ConnectionError(f"source {conn_id!r} unreachable: {exc}") from exc
