"""``canonic gc`` — prune local state under ``.canonic/`` that has outlived its retention."""

from __future__ import annotations

import json
import shutil
from typing import TYPE_CHECKING, Annotated

import typer
from rich.console import Console

from canonic.cli._errors import get_cli_context, handle_errors
from canonic.config import ConfigError, EventLogConfig, find_project_root, load_config
from canonic.ingestion.pending import expired_pending_runs
from canonic.instrumentation.events import expired_event_segments

if TYPE_CHECKING:
    from pathlib import Path

_console = Console(soft_wrap=True)


def _size(path: Path) -> int:
    if path.is_file():
        return path.stat().st_size
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file())


@handle_errors
def gc(
    ctx: typer.Context,
    apply: Annotated[
        bool,
        typer.Option("--apply", help="Delete the listed files. Without it, only list them."),
    ] = False,
    pending_diffs_older_than: Annotated[
        int | None,
        typer.Option(
            "--pending-diffs-older-than",
            min=1,
            help="Also prune fully reviewed pending-diffs runs older than N days.",
        ),
    ] = None,
) -> None:
    """Prune event-log segments beyond ``instrumentation.events`` retention, and old reviewed runs.

    Dry-run by default. Event segments follow ``retention_days`` and ``max_segments`` from
    canonic.yaml (nothing is removed when both are unset). Pending-diffs runs are audit
    artifacts, so they are only touched with ``--pending-diffs-older-than`` and never while
    a proposal is still pending.
    """
    json_output = get_cli_context(ctx).json_output
    root = find_project_root()
    if root is None:
        msg = "no canonic project found; run from inside a project directory"
        if json_output:
            typer.echo(json.dumps({"error": msg}))
        else:
            _console.print(f"[red]error:[/red] {msg}")
        raise typer.Exit(1)

    try:
        policy = load_config(root / "canonic.yaml").instrumentation.events
    except ConfigError:
        policy = EventLogConfig()

    targets: dict[str, list[Path]] = {"event_segments": expired_event_segments(root, policy)}
    if pending_diffs_older_than is not None:
        targets["pending_diffs"] = expired_pending_runs(root, pending_diffs_older_than)

    report = {
        kind: [{"path": str(p.relative_to(root)), "bytes": _size(p)} for p in paths]
        for kind, paths in targets.items()
    }
    if apply:
        for paths in targets.values():
            for path in paths:
                if path.is_dir():
                    shutil.rmtree(path)
                else:
                    path.unlink(missing_ok=True)

    if json_output:
        typer.echo(json.dumps({"applied": apply, **report}))
        return

    if not any(report.values()):
        _console.print("nothing to prune")
        return
    verb = "removed" if apply else "would remove"
    for items in report.values():
        for item in items:
            _console.print(f"{verb} {item['path']} ({item['bytes']} bytes)")
    if not apply:
        _console.print("dry run, pass --apply to delete")
