"""Render a :class:`BaselineReport` to the published markdown baseline (SPEC-E10 §7, S9)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from canon.eval.models import StructuredOutcome
from canon.runtime.resolver import Task

if TYPE_CHECKING:
    from canon.eval.models import BaselineReport, ModelTaskSummary

__all__ = ["render_markdown"]

_OUTCOME_LABELS: dict[StructuredOutcome, str] = {
    StructuredOutcome.HONORED: "honored",
    StructuredOutcome.SCHEMA_INVALID: "schema-invalid",
    StructuredOutcome.UNSUPPORTED: "unsupported",
    StructuredOutcome.ERROR: "error",
}


def _pct(value: float) -> str:
    return f"{round(value * 100)}%"


def _structured_cell(summary: ModelTaskSummary) -> str:
    """One-cell breakdown of structured-output behavior, dropping zero buckets."""
    parts = [
        f"{_OUTCOME_LABELS[outcome]} {count}/{summary.n}"
        for outcome, count in summary.outcome_counts.items()
        if count
    ]
    return "; ".join(parts) if parts else "—"


def _tokens_cell(summary: ModelTaskSummary) -> str:
    return "—" if summary.median_total_tokens is None else str(summary.median_total_tokens)


def render_markdown(report: BaselineReport) -> str:
    """Render the per-task baseline table, recommended pairing, and re-run instructions."""
    generated = report.generated_at.isoformat(timespec="seconds")
    lines: list[str] = [
        "# Canon local-model baseline",
        "",
        f"_Generated {generated} — re-run with `canon eval baseline` (SPEC-E10 §7, GH-66)._",
        "",
        "Measures the LLM-in-loop **drafting** that feeds compilable semantics — not literal",
        "compiler quality, since the E5 compiler is deterministic and LLM-free. Per task and",
        "model: grain accuracy on a labeled set, structured (JSON-schema) output behavior, and",
        f"latency. Recommended = most accurate model clearing {_pct(report.adherence_floor)}",
        "structured-output adherence.",
        "",
        f"## Task: `{report.task.value}` (grain inference)",
        "",
        "| Model | Accuracy | Structured output | p50 latency | Median tokens | Recommended |",
        "| --- | --- | --- | --- | --- | --- |",
    ]

    for summary in report.summaries:
        recommended = "✅" if summary.name == report.recommended else ""
        correct = round(summary.accuracy * summary.n)
        lines.append(
            f"| {summary.name} (`{summary.model}`) "
            f"| {_pct(summary.accuracy)} ({correct}/{summary.n}) "
            f"| {_structured_cell(summary)} "
            f"| {summary.p50_latency_ms:.0f} ms "
            f"| {_tokens_cell(summary)} "
            f"| {recommended} |"
        )

    lines.append("")
    if report.recommended is not None:
        lines.append(f"**Recommended for `{report.task.value}`:** {report.recommended}.")
    else:
        lines.append(
            f"**No candidate cleared the {_pct(report.adherence_floor)} structured-output "
            "floor** for this task — none is recommended."
        )

    lines += [
        "",
        f"## Task: `{Task.RECONCILE.value}`",
        "",
        "Pending E4 reconciliation drafting — `reconcile` has no live call site yet, so there is",
        "no real behavior to score. The harness is generic and will cover it once E4 wires the",
        "reconcile path (SPEC-E10 §7, GH-66).",
        "",
        "## How to re-run",
        "",
        "Regenerate before tagging a release so the baseline tracks reality as models churn:",
        "",
        "```bash",
        "canon eval baseline --candidates candidates.yaml --out docs/baseline-models.md",
        "```",
        "",
        "`--candidates` is a YAML list of `openai_compatible` models (see",
        "`examples/eval/candidates.example.yaml`); `--dataset` defaults to the shipped labeled set.",
        "",
    ]
    return "\n".join(lines)
