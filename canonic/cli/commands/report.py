"""``canonic report`` — surface event-log figures from the local ``.canonic/`` store (SPEC-E16 §4)."""

from __future__ import annotations

import contextlib
import json
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from canonic.cli._errors import get_cli_context, handle_errors
from canonic.config import ConfigError, find_project_root, load_config
from canonic.instrumentation.models import FunnelMilestone
from canonic.instrumentation.report import FunnelReport, build_funnel, build_report, read_events

_console = Console(soft_wrap=True)

_MILESTONE_LABELS: dict[str, str] = {
    FunnelMilestone.SETUP_STARTED: "setup_started",
    FunnelMilestone.CONNECTION_ADDED: "connection_added",
    FunnelMilestone.BOOTSTRAP_COMPLETED: "bootstrap_completed",
    FunnelMilestone.FIRST_ANSWER_SERVED: "first_answer_served",
    FunnelMilestone.FIRST_CURATED_REVIEW_COMPLETED: "first_curated_review_completed",
}


def _render_funnel(funnel: FunnelReport) -> None:
    if not funnel.reached:
        return
    _console.print("[bold]onboarding funnel[/bold]")
    for value in _MILESTONE_LABELS.values():
        ts = funnel.milestones.get(value)
        marker = "[green]✓[/green]" if ts else "[dim]·[/dim]"
        ts_str = f"  {ts}" if ts else ""
        _console.print(f"  {marker} {value}{ts_str}")
    if funnel.time_to_first_answer_seconds is not None:
        _console.print(
            f"  time-to-first-answer: [bold]{funnel.time_to_first_answer_seconds:.1f}s[/bold]"
        )
    _console.print()


@handle_errors
def report(
    ctx: typer.Context,
    last: Annotated[
        int | None,
        typer.Option("--last", help="Restrict to the final N events in the log.", min=1),
    ] = None,
    recent: Annotated[
        int,
        typer.Option("--recent", help="Number of recent answers to list.", min=1),
    ] = 10,
) -> None:
    """Show event-log figures: counts, error distribution, latency, bytes scanned, and freshness."""
    json_output = get_cli_context(ctx).json_output
    root = find_project_root()

    if root is None:
        if json_output:
            typer.echo(json.dumps({"project_root": None}))
        else:
            _console.print("no canonic project found")
        return

    telemetry_enabled: bool = False
    with contextlib.suppress(ConfigError):
        telemetry_enabled = load_config(root / "canonic.yaml").telemetry.enabled

    events = read_events(root, last=last, kind="served_answer")
    rep = build_report(events, recent=recent)
    funnel_events = read_events(root, kind="funnel_milestone")
    funnel = build_funnel(funnel_events)

    if json_output:
        payload = rep.model_dump(mode="json")
        payload["telemetry_enabled"] = telemetry_enabled
        payload["funnel"] = funnel.model_dump(mode="json")
        typer.echo(json.dumps(payload))
        return

    _console.print(
        f"[bold]canonic report[/bold]  (telemetry: {'on' if telemetry_enabled else 'off'})"
    )
    _console.print()

    _render_funnel(funnel)

    if rep.count == 0:
        _console.print("[yellow]no served answers recorded yet[/yellow]")
        return

    _console.print(f"answers:        [bold]{rep.count}[/bold]  ({rep.first_ts} → {rep.last_ts})")

    if rep.latency is not None:
        lat = rep.latency
        _console.print(
            f"latency:        p50 {lat.p50_ms}ms  p95 {lat.p95_ms}ms"
            f"  min {lat.min_ms}ms  max {lat.max_ms}ms  avg {lat.avg_ms:.0f}ms"
        )

    if rep.bytes_scanned is not None:
        b = rep.bytes_scanned
        _console.print(
            f"bytes scanned:  total {b.total:,}  min {b.min:,}  max {b.max:,}  avg {b.avg:,.0f}"
        )
    else:
        _console.print("bytes scanned:  n/a")

    _console.print(f"stale answers:  {rep.stale_answers}")
    _console.print(f"guardrail hits: {rep.guardrail_coverage}")
    _console.print()

    err_table = Table(title="Error distribution", show_header=True, header_style="bold")
    err_table.add_column("code")
    err_table.add_column("count", justify="right")
    for code, count in sorted(rep.error_distribution.items()):
        style = "green" if code == "ok" else "red"
        err_table.add_row(f"[{style}]{code}[/{style}]", str(count))
    _console.print(err_table)

    if rep.recent:
        _console.print()
        recent_table = Table(
            title=f"Last {len(rep.recent)} answers", show_header=True, header_style="bold"
        )
        recent_table.add_column("timestamp")
        recent_table.add_column("result")
        recent_table.add_column("latency_ms", justify="right")
        recent_table.add_column("bytes_scanned", justify="right")
        for event in rep.recent:
            result_str = event.error if event.error is not None else "ok"
            style = "green" if event.error is None else "red"
            bytes_str = f"{event.bytes_scanned:,}" if event.bytes_scanned is not None else "n/a"
            recent_table.add_row(
                event.ts,
                f"[{style}]{result_str}[/{style}]",
                str(event.latency_ms),
                bytes_str,
            )
        _console.print(recent_table)
