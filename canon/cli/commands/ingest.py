"""``canon ingest`` — run the four-stage ingestion pipeline (SPEC-E4 §2, §7, §8).

Refreshes context from the configured sources: introspects each connection into normalized
evidence, drafts proposals, reconciles them against the accepted files, validates the proposed
state, and emits reviewable diffs plus a ``ReconciliationReport``. Propose-only by default
(§5.5) — it writes the audit trail and refreshes ``last_validated_at`` on unchanged files but
edits no committed semantics in place. ``--bootstrap`` is the fast initial path for a fresh
connection (§8); ``--dry-run`` computes and prints diffs while touching nothing.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Annotated

import typer

from canon.cli._errors import get_cli_context, handle_errors
from canon.cli.commands import _console
from canon.config import find_project_root, load_config
from canon.connectors.factory import connector_for
from canon.exc import ConnectionError
from canon.ingestion.models import ReconciliationDecision
from canon.ingestion.pipeline import IngestionPipeline
from canon.ingestion.source import evidence_from_introspection

if TYPE_CHECKING:
    from pathlib import Path

    from canon.config import CanonConfig, Connection
    from canon.connectors.base import ConnectorBase
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
) -> None:
    """Reconcile configured sources into reviewable context diffs (SPEC-E4 §2)."""
    root = find_project_root()
    if root is None:
        _console.print(
            "[red]error:[/red] no canon project found — run from inside a project directory"
        )
        raise typer.Exit(1)

    config = load_config(root / "canon.yaml")
    targets = _select_connections(config, bootstrap=bootstrap, connection=connection)
    result = asyncio.run(_ingest(root, config, targets, bootstrap=bootstrap, dry_run=dry_run))

    if get_cli_context(ctx).json_output:
        typer.echo(result.emission.to_json())
    else:
        typer.echo(result.emission.render_markdown())

    if config.reconcile.strict_contradictions:
        contradictions = result.report.summary[ReconciliationDecision.CONTRADICTION.value]
        if contradictions > 0:
            # SPEC-E4 §5.4: strict mode gates the run on contradictions. No dedicated error
            # code exists yet (a future additive); exit non-zero so CI fails the run.
            raise typer.Exit(1)


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
) -> PipelineResult:
    """Build connectors, gather evidence, and drive the pipeline; always closes connectors."""
    connectors: dict[str, ConnectorBase] = {conn.id: connector_for(conn) for conn in targets}
    pipeline = IngestionPipeline(root, connectors, config.reconcile)
    try:
        if bootstrap:
            return await pipeline.bootstrap(targets[0].id)
        evidence = [
            item
            for conn in targets
            for item in await evidence_from_introspection(connectors[conn.id], conn.id)
        ]
        return await pipeline.run(evidence, dry_run=dry_run)
    finally:
        for connector in connectors.values():
            await connector.aclose()
