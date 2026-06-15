"""Stage 3 — join-graph planning (SPEC-E5-E15 §4 step 3, §9 S4).

From the metric's owning source, find the join path to every referenced source using
only declared ``joins``. No path → ``UNREACHABLE``; more than one valid path →
``AMBIGUOUS_JOIN_PATH``. The compiler never invents a cross join and never guesses a
shortest path (SPEC §10, decided).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from canon.exc import AmbiguousJoinPath, Unreachable

if TYPE_CHECKING:
    from collections.abc import Iterable

    from canon.semantic.models import Join, SemanticSource

__all__ = ["JoinEdge", "plan_joins"]


@dataclass(frozen=True, slots=True)
class JoinEdge:
    """One declared join traversed from ``from_source`` to ``join.to``."""

    from_source: str
    join: Join


def _all_simple_paths(
    owner: str,
    target: str,
    sources_by_name: dict[str, SemanticSource],
) -> list[list[JoinEdge]]:
    """Enumerate every simple directed path of declared joins from owner to target."""
    paths: list[list[JoinEdge]] = []

    def walk(node: str, visited: frozenset[str], trail: list[JoinEdge]) -> None:
        if node == target:
            paths.append(list(trail))
            return
        source = sources_by_name.get(node)
        if source is None:
            return
        for join in source.joins:  # declared order → deterministic enumeration
            if join.to in visited:
                continue
            trail.append(JoinEdge(from_source=node, join=join))
            walk(join.to, visited | {join.to}, trail)
            trail.pop()

    walk(owner, frozenset({owner}), [])
    return paths


def plan_joins(
    owner: str,
    targets: Iterable[str],
    sources_by_name: dict[str, SemanticSource],
) -> list[JoinEdge]:
    """Return the ordered join edges connecting ``owner`` to every target source.

    Targets are processed in sorted order for determinism; an edge whose target is
    already joined is skipped so no source is joined twice. Raises :class:`Unreachable`
    when a target has no path and :class:`AmbiguousJoinPath` when it has more than one.
    """
    edges: list[JoinEdge] = []
    joined: set[str] = {owner}
    for target in sorted(set(targets)):
        if target in joined:
            continue
        paths = _all_simple_paths(owner, target, sources_by_name)
        if not paths:
            raise Unreachable(f"source {target!r} has no declared join path from {owner!r}")
        if len(paths) > 1:
            raise AmbiguousJoinPath(
                f"more than one join path from {owner!r} to {target!r}; "
                f"an explicit path is required",
                candidates=[[e.join.to for e in path] for path in paths],
            )
        for edge in paths[0]:
            if edge.join.to not in joined:
                edges.append(edge)
                joined.add(edge.join.to)
    return edges
