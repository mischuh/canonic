"""Pure helpers for rendering plain-language sample questions (SPEC-E7/E8 §4.1 S12).

Separated from the service so they are unit-testable without a CanonicService instance.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from canonic.contracts.models import Example

__all__ = ["questions_for_group", "render_question"]


def render_question(metric: str, dimensions: list[str]) -> str:
    """Render a single plain-language question for *metric* over *dimensions*.

    Underscores in metric/dimension names are replaced with spaces for readability.
    When no dimensions are provided, a total question is emitted.
    """
    m = metric.replace("_", " ")
    if not dimensions:
        return f"What is the total {m}?"
    dim_labels = " and ".join(d.replace("_", " ") for d in dimensions)
    return f"What was {m} by {dim_labels}?"


def questions_for_group(
    metrics_with_examples: list[tuple[str, list[Example]]],
    dimensions: list[str],
    *,
    max_questions: int = 3,
) -> list[str]:
    """Return ≤ *max_questions* plain-language sample questions for a domain group.

    Priority:
    1. Render each metric's ``Example`` entries (highest frequency first), deduplicated.
    2. If no examples exist across the entire group, emit one templated fallback from
       ``render_question(first_metric, first_dim)`` — never returns an empty list (AC3).
    """
    seen: set[str] = set()
    questions: list[str] = []

    for metric, examples in metrics_with_examples:
        sorted_examples = sorted(
            examples,
            key=lambda e: e.frequency if e.frequency is not None else 0,
            reverse=True,
        )
        for ex in sorted_examples:
            if len(questions) >= max_questions:
                break
            q = render_question(metric, list(ex.query.dimensions))
            if q not in seen:
                seen.add(q)
                questions.append(q)
        if len(questions) >= max_questions:
            break

    if not questions:
        first_metric = metrics_with_examples[0][0] if metrics_with_examples else "metric"
        first_dim = [dimensions[0]] if dimensions else []
        questions.append(render_question(first_metric, first_dim))

    return questions
