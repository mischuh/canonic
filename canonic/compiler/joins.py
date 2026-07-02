"""Stage 3 — join-graph planning (SPEC-E5-E15 §4 step 3, §9 S4).

From the metric's owning source, find the join path to every referenced source using
only declared ``joins``. No path → ``UNREACHABLE``; more than one valid path →
``AMBIGUOUS_JOIN_PATH``. The compiler never invents a cross join and never guesses a
shortest path (SPEC §10, decided).

Named joins (``join.name``) allow the same source to be joined under multiple SQL
aliases (e.g. ``pickup`` and ``dropoff`` for a car-rental model). The traversal tracks
aliases rather than source names so both paths can coexist in a single query.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import sqlglot
from pydantic import BaseModel, ConfigDict
from sqlglot import exp

from canonic.exc import AmbiguousJoinPath, Unreachable

if TYPE_CHECKING:
    from collections.abc import Iterable

    from canonic.semantic.models import Join, SemanticSource

__all__ = [
    "JoinEdge",
    "JoinPathCandidate",
    "build_alias_tree",
    "plan_joins",
    "reachable_dimension_names",
]


@dataclass(frozen=True, slots=True)
class JoinEdge:
    """One declared join traversed from ``from_source`` / ``from_alias`` to ``alias``.

    ``alias`` is the SQL table alias for the join target (``join.name or join.to``).
    ``on_sql`` is the rewritten ON clause with all table references replaced by aliases.
    """

    from_source: str
    from_alias: str
    join: Join
    alias: str
    on_sql: str


class JoinPathCandidate(BaseModel):
    """A candidate join path for an ambiguous query, ready to act on.

    Returned in ``AmbiguousJoinPath.candidates`` so MCP/CLI clients can present
    concrete choices. ``via`` is the exact value to pass back in ``SemanticQuery.via``
    to select this path; ``route`` is a human-readable rendering for display.
    """

    model_config = ConfigDict(frozen=True)

    via: list[str]
    route: str
    joins: list[dict[str, Any]]


def build_alias_tree(
    owner: str,
    sources_by_name: dict[str, SemanticSource],
) -> dict[str, str]:
    """Return ``{alias: source_name}`` for every alias reachable from *owner*.

    For unnamed joins the alias equals the source name. For named joins (``join.name``
    set) the alias is the declared name. The owner itself maps to its own name.
    """
    alias_to_source: dict[str, str] = {owner: owner}
    queue: list[tuple[str, str]] = [(owner, owner)]  # (alias, source_name)
    visited: set[str] = {owner}
    while queue:
        _, src_name = queue.pop(0)
        src = sources_by_name.get(src_name)
        if src is None:
            continue
        for join in src.joins:
            child_alias = join.name or join.to
            if child_alias not in visited:
                visited.add(child_alias)
                alias_to_source[child_alias] = join.to
                queue.append((child_alias, join.to))
    return alias_to_source


def reachable_dimension_names(
    source_name: str,
    sources_by_name: dict[str, SemanticSource],
) -> list[tuple[str, str]]:
    """Return ``(qualified_dim_name, alias)`` for every dimension reachable from *source_name*.

    Traverses the join graph breadth-first. When a dimension name appears under exactly
    one alias the name is returned unqualified; when it appears under more than one alias
    it is returned as ``"alias.dim"`` — the same string a caller can pass to ``query()``.

    Used by the compiler to compute ``metadata.related.unused_dimensions``; also used by
    :meth:`canonic.core.service.CanonicService._reachable_dimensions` to avoid duplicating
    the traversal logic.
    """
    alias_to_source = build_alias_tree(source_name, sources_by_name)

    all_dims: list[tuple[str, str]] = []  # (alias, dim_name)
    seen_aliases: set[str] = set()
    queue: list[str] = [source_name]
    while queue:
        alias = queue.pop(0)
        if alias in seen_aliases:
            continue
        seen_aliases.add(alias)
        src_name = alias_to_source.get(alias, alias)
        src = sources_by_name.get(src_name)
        if src is None:
            continue
        for d in src.dimensions:
            all_dims.append((alias, d.name))
        for join in src.joins:
            child_alias = join.name or join.to
            if child_alias not in seen_aliases:
                queue.append(child_alias)

    dim_aliases: dict[str, list[str]] = {}
    for alias, dim_name in all_dims:
        dim_aliases.setdefault(dim_name, []).append(alias)

    seen_result: set[str] = set()
    result: list[tuple[str, str]] = []
    for alias, dim_name in all_dims:
        entry_name = f"{alias}.{dim_name}" if len(dim_aliases[dim_name]) > 1 else dim_name
        if entry_name not in seen_result:
            seen_result.add(entry_name)
            result.append((entry_name, alias))
    return result


def _rewrite_on(
    on_sql: str,
    from_src: str,
    from_alias: str,
    to_src: str,
    to_alias: str,
    from_table: str | None = None,
    to_table: str | None = None,
) -> str:
    """Rewrite ON clause table references to use SQL aliases instead of source names.

    Accepts references written as either source names (e.g. ``orders``) or the
    physical table name without schema (e.g. ``fct_orders``).
    """
    from_names = {n for n in (from_src, from_table) if n}
    to_names = {n for n in (to_src, to_table) if n}

    parsed = sqlglot.parse_one(on_sql)

    def transform(node: exp.Expression) -> exp.Expression:
        if isinstance(node, exp.Column):
            if node.table in from_names:
                return exp.column(node.name, table=from_alias)
            if node.table in to_names:
                return exp.column(node.name, table=to_alias)
        return node

    return parsed.transform(transform).sql()


def _all_simple_paths(
    owner: str,
    target: str,
    sources_by_name: dict[str, SemanticSource],
) -> list[list[JoinEdge]]:
    """Enumerate every simple directed path of declared joins from owner to target alias."""
    paths: list[list[JoinEdge]] = []

    def walk(
        node_alias: str,
        node_src: str,
        visited: frozenset[str],
        trail: list[JoinEdge],
    ) -> None:
        if node_alias == target:
            paths.append(list(trail))
            return
        source = sources_by_name.get(node_src)
        if source is None:
            return
        for join in source.joins:  # declared order → deterministic enumeration
            child_alias = join.name or join.to
            if child_alias in visited:
                continue
            from_table = source.table.split(".")[-1]
            child_source = sources_by_name.get(join.to)
            to_table = child_source.table.split(".")[-1] if child_source else join.to
            on_sql = _rewrite_on(
                join.on, node_src, node_alias, join.to, child_alias, from_table, to_table
            )
            trail.append(
                JoinEdge(
                    from_source=node_src,
                    from_alias=node_alias,
                    join=join,
                    alias=child_alias,
                    on_sql=on_sql,
                )
            )
            walk(child_alias, join.to, visited | {child_alias}, trail)
            trail.pop()

    walk(owner, owner, frozenset({owner}), [])
    return paths


def _filter_by_via(paths: list[list[JoinEdge]], via: list[str]) -> list[list[JoinEdge]]:
    """Keep only paths whose full alias sequence starts with ``via`` as a prefix.

    The alias sequence is ``[e.alias for e in path]``, which includes all joined aliases
    from the first join to the final target. A path matches if its aliases begin with
    the exact sequence specified in ``via``.
    """
    result = []
    for path in paths:
        full_path = [e.alias for e in path]
        if len(via) <= len(full_path) and full_path[: len(via)] == via:
            result.append(path)
    return result


def plan_joins(
    owner: str,
    targets: Iterable[str],
    sources_by_name: dict[str, SemanticSource],
    via: list[str] | None = None,
) -> list[JoinEdge]:
    """Return the ordered join edges connecting ``owner`` to every target alias.

    Targets are processed in sorted order for determinism; an edge whose target alias is
    already joined is skipped so no alias is joined twice. Raises :class:`Unreachable`
    when a target has no path and :class:`AmbiguousJoinPath` when it has more than one.

    ``via`` is an optional prefix of alias names that each ambiguous join path
    must begin with. When ``via`` is provided and narrows the candidate set
    to exactly one path, that path is used. If it eliminates all paths, :class:`Unreachable`
    is raised instead.
    """
    edges: list[JoinEdge] = []
    joined: set[str] = {owner}
    for target in sorted(set(targets)):
        if target in joined:
            continue
        paths = _all_simple_paths(owner, target, sources_by_name)
        if not paths:
            raise Unreachable(f"source {target!r} has no declared join path from {owner!r}")
        if len(paths) > 1 and via is not None:
            filtered = _filter_by_via(paths, via)
            if not filtered:
                raise Unreachable(
                    f"no join path from {owner!r} to {target!r} passes through {via!r}"
                )
            paths = filtered
        if len(paths) > 1:
            candidates = [
                JoinPathCandidate(
                    via=[e.alias for e in path],
                    route=" → ".join([owner] + [e.alias for e in path]),
                    joins=[
                        {"from": e.from_source, "to": e.join.to, "on": e.on_sql, "alias": e.alias}
                        for e in path
                    ],
                )
                for path in paths
            ]
            raise AmbiguousJoinPath(
                f"more than one join path from {owner!r} to {target!r}; "
                f'use "via" to specify which path',
                owner=owner,
                target=target,
                candidates=candidates,
            )
        for edge in paths[0]:
            if edge.alias not in joined:
                edges.append(edge)
                joined.add(edge.alias)
    return edges
