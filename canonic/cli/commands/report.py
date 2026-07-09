"""``canonic report`` — surface event-log figures from the local ``.canonic/`` store (SPEC-E16 §4)."""

from __future__ import annotations

import contextlib
import json
from typing import TYPE_CHECKING, Annotated

import typer
from rich.console import Console
from rich.table import Table

if TYPE_CHECKING:
    from pathlib import Path

    from canonic.trust.models import TrustScore

from canonic.cli._errors import get_cli_context, handle_errors
from canonic.config import ConfigError, FeedbackConfig, find_project_root, load_config
from canonic.core.service import CanonicService
from canonic.exc import ContractError
from canonic.feedback.history import BindingOutcomeHistory
from canonic.feedback.report import FeedbackReport, build_feedback_report
from canonic.instrumentation.models import FunnelMilestone
from canonic.instrumentation.report import (
    CalibrationReport,
    CorrectionRecurrenceReport,
    FunnelReport,
    build_calibration,
    build_correction_recurrence,
    build_funnel,
    build_report,
    read_events,
)
from canonic.instrumentation.telemetry import build_telemetry_payload
from canonic.trust.models import TrustTier

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


def _load_trust_scores(root: Path) -> list[tuple[str, TrustScore]]:
    """Static trust tiers for every active metric, or [] if the project can't load."""
    with contextlib.suppress(ConfigError, ContractError):
        return CanonicService.from_project(root).trust_report()
    return []


def _render_trust_report(trust_scores: list[tuple[str, TrustScore]]) -> None:
    if not trust_scores:
        return
    order = list(TrustTier)
    ranked = sorted(trust_scores, key=lambda item: order.index(item[1].tier))
    table = Table(title="Trust tier by metric", show_header=True, header_style="bold")
    table.add_column("metric")
    table.add_column("tier")
    table.add_column("reasons")
    tier_style = {
        TrustTier.CAUTION: "red",
        TrustTier.PROVISIONAL: "yellow",
        TrustTier.TRUSTED: "green",
    }
    for metric, score in ranked:
        style = tier_style[score.tier]
        table.add_row(metric, f"[{style}]{score.tier.value}[/{style}]", "; ".join(score.reasons))
    _console.print(table)
    _console.print()


def _render_calibration(calibration: CalibrationReport) -> None:
    if not calibration.buckets:
        return
    table = Table(title="Trust calibration", show_header=True, header_style="bold")
    table.add_column("tier")
    table.add_column("total", justify="right")
    table.add_column("incorrect", justify="right")
    table.add_column("incorrect rate", justify="right")
    tier_style = {
        TrustTier.CAUTION: "red",
        TrustTier.PROVISIONAL: "yellow",
        TrustTier.TRUSTED: "green",
    }
    for bucket in calibration.buckets:
        style = tier_style.get(TrustTier(bucket.tier), "white")
        table.add_row(
            f"[{style}]{bucket.tier}[/{style}]",
            str(bucket.total),
            str(bucket.incorrect),
            f"{bucket.incorrect_rate:.1%}",
        )
    _console.print(table)
    if calibration.unmatched:
        _console.print(f"  ({calibration.unmatched} outcome(s) unmatched to a scored answer)")
    _console.print()


def _render_recurrence(recurrence: CorrectionRecurrenceReport) -> None:
    if not recurrence.entries:
        return
    table = Table(title="Correction recurrence", show_header=True, header_style="bold")
    table.add_column("binding")
    table.add_column("incorrect outcomes", justify="right")
    for entry in recurrence.entries:
        table.add_row(entry.binding, str(entry.count))
    _console.print(table)
    _console.print()


def _render_feedback(feedback: FeedbackReport) -> None:
    if not feedback.entries:
        return
    table = Table(title="Feedback loop", show_header=True, header_style="bold")
    table.add_column("binding")
    table.add_column("wrong_definition", justify="right")
    table.add_column("markers", justify="right")
    table.add_column("gated (E4)")
    table.add_column("trust capped")
    for entry in feedback.entries:
        gated = "[red]yes[/red]" if entry.gated else "no"
        capped = "[red]yes[/red]" if entry.capped else "no"
        table.add_row(
            entry.binding,
            str(entry.wrong_definition_count),
            str(entry.distinct_markers),
            gated,
            capped,
        )
    _console.print(table)
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
    telemetry_preview: Annotated[
        bool,
        typer.Option(
            "--telemetry-preview",
            help="Print exactly the aggregate payload opt-in telemetry would send, "
            "without sending it (SPEC-E16 Part 2 §5).",
        ),
    ] = False,
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
    air_gapped: bool = False
    feedback_config = FeedbackConfig()
    with contextlib.suppress(ConfigError):
        cfg = load_config(root / "canonic.yaml")
        telemetry_enabled = cfg.telemetry.enabled
        air_gapped = cfg.runtime.air_gapped
        feedback_config = cfg.feedback

    events = read_events(root, last=last, kind="served_answer")
    rep = build_report(events, recent=recent)
    funnel_events = read_events(root, kind="funnel_milestone")
    funnel = build_funnel(funnel_events)
    outcome_events = read_events(root, kind="answer_outcome")
    calibration = build_calibration(events, outcome_events)
    recurrence = build_correction_recurrence(events, outcome_events)
    outcome_history = BindingOutcomeHistory.from_events(events, outcome_events)
    feedback = build_feedback_report(outcome_history, feedback_config)

    if telemetry_preview:
        payload = build_telemetry_payload(rep, calibration, recurrence, funnel)
        if json_output:
            typer.echo(json.dumps(payload))
            return
        status = "on" if telemetry_enabled else "off"
        if air_gapped:
            status += ", forced off (air-gapped)"
        _console.print(f"[bold]telemetry preview[/bold]  (telemetry: {status})")
        _console.print("this is exactly what would be sent — nothing is sent by this command")
        _console.print_json(json.dumps(payload))
        return

    trust_scores = _load_trust_scores(root)

    if json_output:
        payload = rep.model_dump(mode="json")
        payload["telemetry_enabled"] = telemetry_enabled
        payload["funnel"] = funnel.model_dump(mode="json")
        payload["trust"] = [
            {"metric": metric, "tier": score.tier.value, "reasons": list(score.reasons)}
            for metric, score in trust_scores
        ]
        payload["calibration"] = calibration.model_dump(mode="json")
        payload["correction_recurrence"] = recurrence.model_dump(mode="json")
        payload["feedback"] = feedback.model_dump(mode="json")
        typer.echo(json.dumps(payload))
        return

    _console.print(
        f"[bold]canonic report[/bold]  (telemetry: {'on' if telemetry_enabled else 'off'})"
    )
    _console.print()

    _render_funnel(funnel)
    _render_trust_report(trust_scores)
    _render_calibration(calibration)
    _render_recurrence(recurrence)
    _render_feedback(feedback)

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
