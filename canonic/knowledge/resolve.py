"""Resolve DocEvidence topic_ref candidates against live semantic entities (SPEC-E3 §5, §3.2).

``DocEvidence.topic_refs`` are documented as *candidates only* — a connector never asserts
they are real semantic entities. This module is the one place that turns a candidate string
into a fully-qualified ``sl_ref`` (or leaves it unresolved for review), shared by every
caller that writes a ``KnowledgePage`` from fetched evidence (``canonic knowledge add``
today; the E4 builder's own doc-evidence handling would reuse this too).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from canonic.semantic.models import SemanticSource

__all__ = ["resolve_topic_refs"]


def resolve_topic_refs(
    topic_refs: list[str], sources: list[SemanticSource]
) -> tuple[list[str], list[str]]:
    """Case-insensitive exact match against each source's measure/dimension names+aliases.

    Returns ``(resolved, unresolved)``, both in ``topic_refs`` order. A resolved entry is
    a fully-qualified ``{connection}.{source}.{member}`` name using the entity's declared
    (canonical) casing, not the candidate's. No fuzzy matching: ``Measure`` carries no
    ``aliases`` field at all (only ``Dimension`` does), and a wrong auto-link is worse than
    an unresolved candidate surfaced for human review — the same reasoning that keeps
    ``topic_refs`` themselves candidate-only rather than asserted.

    A candidate matching more than one source's entity (measure/dimension names are not
    required unique project-wide) resolves to the first match in ``sources`` order —
    deterministic, not "last source wins".
    """
    index: dict[str, str] = {}
    for source in sources:
        base = f"{source.connection}.{source.name}"
        for measure in source.measures:
            index.setdefault(measure.name.lower(), f"{base}.{measure.name}")
        for dimension in source.dimensions:
            index.setdefault(dimension.name.lower(), f"{base}.{dimension.name}")
            for alias in dimension.aliases:
                index.setdefault(alias.lower(), f"{base}.{dimension.name}")

    resolved: list[str] = []
    unresolved: list[str] = []
    for candidate in topic_refs:
        fq_name = index.get(candidate.strip().lower())
        if fq_name is not None:
            resolved.append(fq_name)
        else:
            unresolved.append(candidate)
    return resolved, unresolved
