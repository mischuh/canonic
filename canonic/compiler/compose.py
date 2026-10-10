"""Compose — assemble planned leaves into one statement (AMENDMENT-multi-metric-compose §4).

The unifying rule is the additivity spec's "aggregate first, combine last", applied at
query scope: every leaf has already aggregated itself to the requested dimensions, so
combining them cannot change any number. What is left is to decide which leaves are
really the same query, give them stable names, and join them on a grain they all share.

Three things here are load-bearing and easy to get wrong:

*Fusion.* Two leaves share a CTE only when their whole :class:`~canonic.compiler.leaf.LeafKey`
matches, and are merged into one CTE projecting both measures only when everything but
the measure matches. Loosen that and two metrics with genuinely different populations
collapse onto one plan and report the same wrong number.

*The spine.* Leaves are joined onto a ``UNION`` of their dimension tuples with LEFT JOINs,
not chained ``FULL JOIN``s. Chained ``FULL JOIN ... USING`` has coalescing semantics that
are easy to get subtly wrong past two tables, and SQLite has no ``FULL OUTER JOIN`` below
3.39 — so the readable two-table form would have become a per-dialect divergence in a
place where being wrong is silent. ``UNION`` and ``LEFT JOIN`` behave identically on every
dialect canonic ships.

*Missing rows are NULL.* A dimension value in one leaf and not another yields NULL for the
absent metric, never 0. Absence of rows is not a measured zero, and quietly substituting
one for the other is the class of confidently-wrong-number this project exists to prevent.

*Running totals accumulate last.* A cumulative metric's leaf is an ordinary aggregate to the
requested dimensions. The running total is a window over the spine in the outer SELECT, so
a spine row the leaf has no row for still carries the total forward, without a fill-forward
function SQLite lacks. Filters that only pick which order tuples are shown are applied after
that window, never inside the leaf, or the total would restart at the first visible row.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, cast

from sqlglot import exp
from sqlglot.dialects.dialect import Dialect

from canonic.compiler._helpers import _active_adapter, _alias, _freshness
from canonic.compiler.result import FinalityMetadata
from canonic.exc import UnsupportedMeasure

if TYPE_CHECKING:
    from canonic.compiler.result import (
        CompositionMetadata,
        CumulativeMetadata,
        OpaqueMetadata,
        PartialAdditiveMetadata,
        RecomputeAtGrainMetadata,
    )

if TYPE_CHECKING:
    from collections.abc import Sequence

    from canonic.compiler.leaf import AuxCte, LeafMetric, LeafPlan
    from canonic.compiler.result import FiredGuardrail, SourceFreshness
    from canonic.semantic.models import SemanticSource

__all__ = [
    "VISIBLE_CTE",
    "Accumulate",
    "Combine",
    "ComposeResult",
    "LeafRef",
    "MetricLeaves",
    "MetricPlan",
    "compose",
]

#: Name of the grain spine CTE, and the ``_leaf_<i>`` prefix. Underscore-prefixed to match
#: every other compiler-generated identifier (``_base``, ``_ranked``) and so a semantic
#: source legitimately named ``grain`` cannot be shadowed inside its own leaf body.
_GRAIN = "_grain"
_LEAF = "_leaf_"
_IS_FINAL = "is_final"

#: Suffix of the CTE a cumulative leaf hoists with the order tuples its visibility filters
#: keep. Compose declares it under the leaf's own prefix, so the full name is
#: ``_leaf_<i>__visible``.
VISIBLE_CTE = "visible"
_ACCUMULATED = "_accumulated"
_VISIBLE_GRAIN = "_visible_grain"
_ORDER_RANK = "_order_rank"
_FIRST_RANK = "_first_rank"


class Combine(StrEnum):
    """How a requested metric's value is assembled from its leaf columns."""

    DIRECT = "direct"
    RATIO_NULL = "ratio_null"  # n / NULLIF(d, 0)
    RATIO_ZERO = "ratio_zero"  # COALESCE(n / NULLIF(d, 0), 0)
    RATIO_RAW = "ratio_raw"  # n / d, the engine raises on zero


@dataclass(frozen=True, slots=True)
class LeafRef:
    """A column of one planned leaf, addressed before physical CTE names are assigned."""

    leaf: int  # index into the planned leaf list handed to compose()
    column: str


@dataclass(frozen=True, slots=True)
class Accumulate:
    """How a cumulative metric's per-tuple values become a running total over the spine.

    ``order_by`` and ``partition_by`` are output column names of the requested dimensions.
    Together they are every requested dimension, so each order tuple occurs once per
    partition and the ``ROWS`` frame is deterministic. ``fill_observed`` densifies every
    partition to the order tuples observed in the leaf. ``visibility`` is set when the leaf
    hoists a :data:`VISIBLE_CTE` that decides which order tuples are shown.
    """

    order_by: tuple[str, ...]
    partition_by: tuple[str, ...]
    fill_observed: bool = False
    visibility: bool = False


@dataclass(frozen=True, slots=True)
class MetricPlan:
    """One requested metric: where its value comes from, and how it is combined."""

    name: str
    refs: tuple[LeafRef, ...]
    combine: Combine = Combine.DIRECT
    accumulate: Accumulate | None = None


@dataclass(frozen=True, slots=True)
class MetricLeaves:
    """What one requested metric contributes to the query: its leaves and its expression.

    Every ``kind`` produces one of these, which is what lets the router treat a ratio, a
    semi-additive collapse and a plain sum identically once planned. ``metric.refs`` index
    into ``leaves``; the router offsets them as it concatenates each metric's leaves into
    the query's leaf list.
    """

    leaves: list[LeafPlan]
    metric: MetricPlan
    resolved: str
    composition: CompositionMetadata | None = None
    partial_additive: PartialAdditiveMetadata | None = None
    recompute_at_grain: RecomputeAtGrainMetadata | None = None
    opaque: OpaqueMetadata | None = None
    cumulative: CumulativeMetadata | None = None
    warnings: tuple[str, ...] = ()

    def offset(self, by: int) -> MetricPlan:
        """Re-point this metric's leaf references into the whole query's leaf list."""
        return MetricPlan(
            name=self.metric.name,
            refs=tuple(LeafRef(leaf=r.leaf + by, column=r.column) for r in self.metric.refs),
            combine=self.metric.combine,
            accumulate=self.metric.accumulate,
        )


@dataclass(frozen=True, slots=True)
class ComposeResult:
    """The composed statement plus the query-scope metadata merge (AMENDMENT §7)."""

    ast: exp.Expression
    guardrails_fired: list[FiredGuardrail]
    warnings: list[str]
    freshness: list[SourceFreshness]
    finality: FinalityMetadata | None
    cte_count: int
    scoped_sources: frozenset[str] = frozenset()
    shared_sources: frozenset[str] = frozenset()


@dataclass(slots=True)
class _Physical:
    """One emitted CTE: the leaves fused into it, and the columns it projects."""

    key_leaf: LeafPlan  # the leaf whose plan (and rebuild) this CTE is built from
    metrics: list[LeafMetric] = field(default_factory=list)
    columns: list[str] = field(default_factory=list)
    fired: list[FiredGuardrail] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    used_sources: set[str] = field(default_factory=set)
    scoped_sources: set[str] = field(default_factory=set)
    shared_sources: set[str] = field(default_factory=set)
    finality: FinalityMetadata | None = None

    @property
    def projects_is_final(self) -> bool:
        return self.key_leaf.projects_is_final


def _fuse(leaves: Sequence[LeafPlan], *, dedup: bool) -> tuple[list[_Physical], list[int]]:
    """Collapse planned leaves into the CTEs that will actually be emitted (§3.2).

    Returns the physical CTEs and, per planned leaf, the index of the CTE serving it.
    With ``dedup=False`` every planned leaf becomes its own CTE — the emitted numbers must
    be identical either way, which is what makes fusion checkable rather than trusted.
    """
    physical: list[_Physical] = []
    by_fusion_key: dict[tuple[object, ...], int] = {}
    owner_of: list[int] = []

    for leaf in leaves:
        # A leaf whose builder wraps exactly one measure can still deduplicate against an
        # identical leaf, but must never absorb a different one — so it groups on its full
        # key rather than the measure-blind fusion key.
        fusion_key = leaf.key.fusion_key if leaf.fusable else leaf.key.sort_key
        index = by_fusion_key.get(fusion_key) if dedup else None
        if index is None:
            index = len(physical)
            physical.append(_Physical(key_leaf=leaf))
            if dedup:
                by_fusion_key[fusion_key] = index
        target = physical[index]
        for leaf_metric, column in zip(leaf.metrics, leaf.measure_aliases, strict=True):
            # An identical column already projected is the dedup case proper: two metrics
            # resolving to the same plan *and* the same measure need one column, not two.
            if column not in target.columns:
                target.columns.append(column)
                target.metrics.append(leaf_metric)
        _merge_into(target, leaf)
        owner_of.append(index)

    return physical, owner_of


def _merge_into(target: _Physical, leaf: LeafPlan) -> None:
    """Union one planned leaf's metadata into the CTE that will serve it."""
    seen_ids = {g.id for g in target.fired}
    target.fired.extend(g for g in leaf.fired if g.id not in seen_ids)
    target.warnings.extend(w for w in leaf.warnings if w not in target.warnings)
    target.used_sources |= leaf.used_sources
    target.scoped_sources |= leaf.scoped_sources
    target.shared_sources |= leaf.shared_sources
    if leaf.finality is not None:
        target.finality = leaf.finality


def _combine_expr(metric: MetricPlan, columns: list[exp.Expression]) -> exp.Expression:
    """Build one requested metric's output expression from its leaf column references."""
    if metric.combine is Combine.DIRECT:
        return columns[0]
    numerator, denominator = columns
    if metric.combine is Combine.RATIO_RAW:
        return cast("exp.Expression", exp.Div(this=numerator, expression=denominator))
    guarded = cast(
        "exp.Expression",
        exp.Div(
            this=numerator,
            expression=exp.func("NULLIF", denominator, exp.Literal.number(0)),
        ),
    )
    if metric.combine is Combine.RATIO_ZERO:
        return cast("exp.Expression", exp.func("COALESCE", guarded, exp.Literal.number(0)))
    return guarded


def _null_safe_join_on(spine: str, leaf: str, columns: Sequence[str]) -> exp.Expression:
    """Match a leaf to the spine on ``columns``, treating NULL as a value.

    ``USING (region)`` would be shorter, but ``USING`` compares with ``=``, and ``=`` is
    never true for NULL. A dimension is NULL whenever a leaf reaches it through a join
    that missed — an order with no line items has a NULL ``sku`` — so a plain equijoin
    silently drops every metric for that group while still showing the group, which is
    exactly the confidently-wrong number this compiler exists to prevent.

    Spelled as ``a = b OR (a IS NULL AND b IS NULL)`` rather than ``IS NOT DISTINCT FROM``
    because Redshift does not implement the latter, and putting a correctness-relevant
    construct behind a per-dialect divergence is how a wrong number reaches exactly one
    warehouse and nobody notices.
    """
    conditions: list[exp.Expression] = []
    for column in columns:
        left = cast("exp.Expression", exp.column(column, table=spine))
        right = cast("exp.Expression", exp.column(column, table=leaf))
        both_null = cast(
            "exp.Expression",
            exp.And(
                this=exp.Is(this=left, expression=exp.null()),
                expression=exp.Is(this=right.copy(), expression=exp.null()),
            ),
        )
        conditions.append(
            cast(
                "exp.Expression",
                exp.Paren(
                    this=exp.Or(
                        this=exp.EQ(this=left.copy(), expression=right),
                        expression=both_null,
                    )
                ),
            )
        )
    return cast("exp.Expression", exp.and_(*conditions))


def _build_spine(
    names: list[str],
    physical: list[_Physical],
    dim_names: list[str],
    *,
    with_is_final: bool,
    extra: Sequence[str] = (),
) -> exp.Expression:
    """UNION every leaf's dimension tuples into the spine every metric is served over.

    A leaf with no finality rule contributes ``TRUE`` for ``is_final``: all of its rows
    are final by definition, so pairing them with both branches of a finality leaf would
    claim a provisional reading the leaf never made.

    ``extra`` names further CTEs projecting exactly ``dim_names``, the rows a cumulative
    metric's ``fill_observed`` adds. They are never combined with finality.
    """
    branches: list[exp.Select] = []
    for name, leaf in zip(names, physical, strict=True):
        projections: list[exp.Expression] = [
            _alias(cast("exp.Expression", exp.column(dim, table=name)), dim) for dim in dim_names
        ]
        if with_is_final:
            marker: exp.Expression = (
                cast("exp.Expression", exp.column(_IS_FINAL, table=name))
                if leaf.projects_is_final
                else cast("exp.Expression", exp.true())
            )
            projections.append(_alias(marker, _IS_FINAL))
        branches.append(exp.Select().select(*projections).from_(exp.to_table(name)))
    branches.extend(_dims_from(name, dim_names) for name in extra)

    spine: exp.Select | exp.Union = branches[0]
    for branch in branches[1:]:
        spine = spine.union(branch, distinct=True)
    return spine


def _merge_finality(physical: Sequence[_Physical]) -> FinalityMetadata | None:
    """Conservative finality merge across leaves (§7): earliest watermark, union of sources.

    Earliest rather than latest: a result is only final as far back as its least-settled
    input, and reporting the latest watermark would claim settlement the data does not have.
    """
    leaf_finalities = [p.finality for p in physical if p.finality is not None]
    if not leaf_finalities:
        return None
    return FinalityMetadata(
        watermark=min(f.watermark for f in leaf_finalities),
        sources_used=sorted({s for f in leaf_finalities for s in f.sources_used}),
        result_flag="per_row",
    )


def compose(
    leaves: Sequence[LeafPlan],
    metrics: Sequence[MetricPlan],
    sources_by_name: dict[str, SemanticSource],
    *,
    dedup: bool = True,
) -> ComposeResult:
    """Assemble planned leaves and their metric expressions into a single statement.

    ``leaves`` are in planning order, which is what :class:`LeafRef` indexes into.
    ``metrics`` are the requested metrics in request order, which is the order their
    columns are emitted in.
    """
    physical, owner_of = _fuse(leaves, dedup=dedup)

    # Stable-sorted by plan identity, then named by sort position: the same query always
    # produces the same CTE names, and a diff of two generated statements stays readable
    # in a way a hash-derived name never is (§6).
    order = sorted(range(len(physical)), key=lambda i: _sortable(physical[i]))
    names_by_index = {original: f"{_LEAF}{position}" for position, original in enumerate(order)}
    sorted_physical = [physical[i] for i in order]
    names = [names_by_index[i] for i in order]

    dim_names = list(leaves[0].dim_names)
    with_is_final = any(p.projects_is_final for p in physical)
    accumulating = [m for m in metrics if m.accumulate is not None]
    if accumulating and with_is_final:
        raise UnsupportedMeasure(
            f"cumulative metric {accumulating[0].name!r} cannot be combined with a metric "
            f"under a finality rule in one query; request them separately"
        )

    # Degenerate case: one CTE whose own projection already *is* the requested output.
    # Emitting it bare rather than wrapping it in a pointless `WITH x AS (...) SELECT *
    # FROM x` is not just cosmetic — it is what keeps a single-metric query, and a
    # multi-metric query whose metrics all fused onto one plan, compiling to exactly the
    # SQL it compiled to before compose existed. It also preserves the top-level UNION ALL
    # shape of a finality leaf, which a CTE wrapper would bury.
    if (
        not accumulating
        and len(physical) == 1
        and _projection_is_already_the_output(metrics, physical[0])
    ):
        only = physical[0]
        lone_select, lone_aux = only.key_leaf.rebuild(only.metrics, "")
        return _result(physical, sources_by_name, _attach(lone_select, lone_aux), 0)

    # Output columns: dimensions in request order, then metrics in request order. This is
    # the one place request order beats sort order, because the caller's column order is
    # part of what it asked for.
    # A running total is a window over the spine, so a cumulative metric always goes
    # through it, even when it is the only leaf.
    single_cte = len(physical) == 1 and not accumulating
    dim_source = names[0] if single_cte else _GRAIN
    visibility = any(cast("Accumulate", m.accumulate).visibility for m in accumulating)
    # When the result is read back by name from the accumulated CTE, each metric gets a
    # positional alias there and its output name only in the final SELECT, so the read never
    # depends on output names being unique or distinct from a dimension name.
    internal = [f"_m{i}" for i in range(len(metrics))] if visibility else None
    projections: list[exp.Expression] = [
        _alias(cast("exp.Expression", exp.column(dim, table=dim_source)), dim) for dim in dim_names
    ]
    for position, metric in enumerate(metrics):
        columns = [
            cast("exp.Expression", exp.column(ref.column, table=names_by_index[owner_of[ref.leaf]]))
            for ref in metric.refs
        ]
        value = _combine_expr(metric, columns)
        if metric.accumulate is not None:
            value = _accumulate_expr(metric.accumulate, value)
        projections.append(_alias(value, internal[position] if internal else metric.name))
    if with_is_final:
        projections.append(
            _alias(cast("exp.Expression", exp.column(_IS_FINAL, table=dim_source)), _IS_FINAL)
        )

    outer = exp.Select().select(*projections)
    if single_cte:
        outer = outer.from_(exp.to_table(names[0]))
    elif dim_names:
        outer = outer.from_(exp.to_table(_GRAIN))
        for name, leaf in zip(names, sorted_physical, strict=True):
            # A finality leaf carries two rows per dimension tuple (final and provisional),
            # so it must also match on is_final or a provisional row would pair with the
            # wrong branch. is_final is emitted as a literal and never NULL, so a plain
            # equality is enough for it.
            on = _null_safe_join_on(_GRAIN, name, dim_names)
            if leaf.projects_is_final:
                on = cast(
                    "exp.Expression",
                    exp.and_(
                        on,
                        exp.EQ(
                            this=exp.column(_IS_FINAL, table=_GRAIN),
                            expression=exp.column(_IS_FINAL, table=name),
                        ),
                    ),
                )
            outer = outer.join(exp.to_table(name), on=on, join_type="LEFT")
    else:
        # No dimensions: every leaf is exactly one row, so there is no grain to align.
        outer = outer.from_(exp.to_table(names[0]))
        for name in names[1:]:
            outer = outer.join(exp.to_table(name), join_type="CROSS")

    # Which leaves a cumulative metric densifies, and which ones decide visibility. Keyed by
    # CTE name, since two metrics fused onto one leaf share both.
    fills: dict[str, Accumulate] = {}
    visible: dict[str, Accumulate] = {}
    for metric in accumulating:
        acc = cast("Accumulate", metric.accumulate)
        leaf_name = names_by_index[owner_of[metric.refs[0].leaf]]
        if acc.fill_observed and acc.partition_by:
            fills.setdefault(leaf_name, acc)
        if acc.visibility:
            visible.setdefault(leaf_name, acc)

    final: exp.Select = outer
    if visible:
        assert internal is not None  # noqa: S101 — visible implies a visibility filter
        renames = [
            *zip(dim_names, dim_names, strict=True),
            *zip(internal, (m.name for m in metrics), strict=True),
        ]
        final = exp.Select().select(
            *(
                _alias(cast("exp.Expression", exp.column(column, table=_ACCUMULATED)), output)
                for column, output in renames
            )
        )
        final = final.from_(exp.to_table(_ACCUMULATED)).join(
            exp.to_table(_VISIBLE_GRAIN),
            on=_null_safe_join_on(_ACCUMULATED, _VISIBLE_GRAIN, dim_names),
            join_type="INNER",
        )

    # Leaves are declared before the spine that reads them, and the spine before the outer
    # SELECT that joins onto it.
    ast: exp.Expression = final
    for name, leaf in zip(names, sorted_physical, strict=True):
        # Each leaf's own inner CTEs are declared immediately before it, under a name
        # scoped to that leaf so two leaves of the same kind cannot collide.
        select, aux = leaf.key_leaf.rebuild(leaf.metrics, f"{name}__")
        for cte in aux:
            ast = cast("exp.Select", ast).with_(cte.name, as_=cte.body)
        ast = cast("exp.Select", ast).with_(name, as_=select)
    fill_names: list[str] = []
    for name, acc in sorted(fills.items()):
        rank_name, fill_name = f"{name}__order_rank", f"{name}__fill"
        rank, fill = _fill_observed(name, rank_name, acc, dim_names)
        ast = cast("exp.Select", ast).with_(rank_name, as_=rank).with_(fill_name, as_=fill)
        fill_names.append(fill_name)
    if not single_cte and dim_names:
        ast = cast("exp.Select", ast).with_(
            _GRAIN,
            as_=_build_spine(
                names,
                sorted_physical,
                dim_names,
                with_is_final=with_is_final,
                extra=fill_names,
            ),
        )
    if visible:
        ast = cast("exp.Select", ast).with_(_ACCUMULATED, as_=outer)
        ast = ast.with_(_VISIBLE_GRAIN, as_=_build_visible_spine(names, visible, fills, dim_names))

    return _result(sorted_physical, sources_by_name, ast, len(physical))


def _dims_from(table: str, dim_names: Sequence[str]) -> exp.Select:
    """``SELECT <dims> FROM table``, the shape of every spine branch."""
    return (
        exp.Select()
        .select(*(_alias(cast("exp.Expression", exp.column(d, table=table)), d) for d in dim_names))
        .from_(exp.to_table(table))
    )


def _order_keys(table: str, order_by: Sequence[str]) -> list[exp.Expression]:
    """Ascending sort keys over the order dimensions, with NULLs last on every dialect.

    Each dimension sorts behind an ``IS NULL`` indicator rather than through ``NULLS LAST``.
    Dialects disagree on where NULLs sort by default, and sqlglot drops ``NULLS LAST``
    where it believes the target already sorts that way, and cannot express it at all
    inside a MySQL window. The indicator reads the same everywhere.

    Behind the indicator every NULL of a dimension is a peer, so where that key puts NULLs
    does not matter. It is set to the target dialect's own default, so sqlglot emits no
    ``NULLS FIRST``/``NULLS LAST`` and has nothing to emulate.
    """
    null_ordering = Dialect.get_or_raise(_active_adapter().dialect).NULL_ORDERING
    nulls_first = null_ordering == "nulls_are_small"
    keys: list[exp.Expression] = []
    for name in order_by:
        column = cast("exp.Expression", exp.column(name, table=table))
        is_null = exp.Case(
            ifs=[
                exp.If(
                    this=exp.Is(this=column.copy(), expression=exp.null()),
                    true=exp.Literal.number(1),
                )
            ],
            default=exp.Literal.number(0),
        )
        keys.append(
            cast("exp.Expression", exp.Ordered(this=is_null, desc=False, nulls_first=nulls_first))
        )
        keys.append(
            cast("exp.Expression", exp.Ordered(this=column, desc=False, nulls_first=nulls_first))
        )
    return keys


def _running(func: exp.Expression, acc: Accumulate) -> exp.Expression:
    """``func`` over the partition from its first order tuple up to the current row."""
    return cast(
        "exp.Expression",
        exp.Window(
            this=func,
            partition_by=[exp.column(p, table=_GRAIN) for p in acc.partition_by] or None,
            order=exp.Order(expressions=_order_keys(_GRAIN, acc.order_by)),
            spec=exp.WindowSpec(
                kind="ROWS", start="UNBOUNDED", start_side="PRECEDING", end="CURRENT ROW"
            ),
        ),
    )


def _accumulate_expr(acc: Accumulate, value: exp.Expression) -> exp.Expression:
    """Running total of ``value`` over the spine, NULL before the partition's first row.

    ``COALESCE`` carries the total across spine rows the leaf has no row for (another
    metric's rows, or rows ``fill_observed`` added). ``COUNT`` tells a row before the
    partition's first observed tuple from one after it: before it nothing was measured, and
    a zero there would be a fabricated number.
    """
    count = _running(exp.Count(this=value.copy()), acc)
    total = _running(
        exp.Sum(this=exp.Coalesce(this=value.copy(), expressions=[exp.Literal.number(0)])), acc
    )
    return cast(
        "exp.Expression",
        exp.Case(
            ifs=[
                exp.If(this=exp.EQ(this=count, expression=exp.Literal.number(0)), true=exp.null())
            ],
            default=total,
        ),
    )


def _fill_observed(
    leaf: str, rank_name: str, acc: Accumulate, dim_names: Sequence[str]
) -> tuple[exp.Expression, exp.Expression]:
    """The rows ``fill_observed`` adds: observed order tuples crossed with observed partitions.

    A partition is filled only from its own first observed tuple on, never before it, and
    up to the leaf's last tuple, which is where the observed tuples end. Tuples are compared
    through a ``DENSE_RANK`` over the order dimensions rather than a row-value comparison,
    which not every dialect has. Only whole observed tuples are used, so densifying
    ``(year, month)`` cannot invent a month that never occurred.
    """
    rank = exp.Select().select(
        *(_alias(cast("exp.Expression", exp.column(d, table=leaf)), d) for d in dim_names),
        _alias(
            cast(
                "exp.Expression",
                exp.Window(
                    this=exp.Anonymous(this="DENSE_RANK"),
                    order=exp.Order(expressions=_order_keys(leaf, acc.order_by)),
                ),
            ),
            _ORDER_RANK,
        ),
    )
    rank = rank.from_(exp.to_table(leaf))

    tuples = (
        exp.Select()
        .select(*(exp.column(o, table=rank_name) for o in acc.order_by))
        .select(exp.column(_ORDER_RANK, table=rank_name))
        .distinct()
        .from_(exp.to_table(rank_name))
    )
    partitions = (
        exp.Select()
        .select(*(exp.column(p, table=rank_name) for p in acc.partition_by))
        .select(
            _alias(
                _min(cast("exp.Expression", exp.column(_ORDER_RANK, table=rank_name))), _FIRST_RANK
            )
        )
        .from_(exp.to_table(rank_name))
        .group_by(*(exp.column(p, table=rank_name) for p in acc.partition_by))
    )
    order_set = set(acc.order_by)
    fill = (
        exp.Select()
        .select(
            *(
                _alias(
                    cast("exp.Expression", exp.column(d, table="t" if d in order_set else "p")), d
                )
                for d in dim_names
            )
        )
        .from_(tuples.subquery("t"))
        .join(partitions.subquery("p"), join_type="CROSS")
        .where(
            exp.GTE(
                this=exp.column(_ORDER_RANK, table="t"),
                expression=exp.column(_FIRST_RANK, table="p"),
            )
        )
    )
    return rank, fill


def _min(column: exp.Expression) -> exp.Expression:
    return cast("exp.Expression", exp.Min(this=column))


def _build_visible_spine(
    names: Sequence[str],
    visible: dict[str, Accumulate],
    fills: dict[str, Accumulate],
    dim_names: Sequence[str],
) -> exp.Expression:
    """The dimension tuples a query with visibility filters shows.

    Every leaf other than a cumulative one with visibility filters already applied every
    query filter, so all of its rows are shown. A cumulative leaf with visibility filters
    shows only the rows, and the filled rows, whose order tuple its ``visible`` CTE kept. A
    row stays when any metric shows it, which is the same union the spine itself is built
    by.
    """
    branches: list[exp.Select] = []
    for name in names:
        sources = [name, *([f"{name}__fill"] if name in fills else [])]
        for source in sources:
            branch = _dims_from(source, dim_names)
            acc = visible.get(name)
            if acc is not None:
                visible_name = f"{name}__{VISIBLE_CTE}"
                branch = branch.join(
                    exp.to_table(visible_name),
                    on=_null_safe_join_on(source, visible_name, acc.order_by),
                    join_type="INNER",
                )
            branches.append(branch)
    spine: exp.Select | exp.Union = branches[0]
    for branch in branches[1:]:
        spine = spine.union(branch, distinct=True)
    return spine


def _attach(select: exp.Expression, aux: Sequence[AuxCte]) -> exp.Expression:
    """Declare a lone leaf's inner CTEs on the statement it is emitted as."""
    ast = select
    for cte in aux:
        ast = cast("exp.Select", ast).with_(cte.name, as_=cte.body)
    return ast


def _projection_is_already_the_output(metrics: Sequence[MetricPlan], physical: _Physical) -> bool:
    """True when a lone CTE's own columns are exactly the requested output columns.

    Requires every metric to be a plain column reference, in the same order and under the
    same names the leaf already projects. A ratio fails this even when both its components
    fused onto one plan, because the division still has to happen somewhere outside.
    """
    if any(m.combine is not Combine.DIRECT for m in metrics):
        return False
    requested = [m.name for m in metrics]
    referenced = [ref.column for m in metrics for ref in m.refs]
    return requested == physical.columns and referenced == physical.columns


def _result(
    physical: Sequence[_Physical],
    sources_by_name: dict[str, SemanticSource],
    ast: exp.Expression,
    cte_count: int,
) -> ComposeResult:
    """Merge every leaf's metadata conservatively and pair it with the composed AST (§7)."""
    return ComposeResult(
        ast=ast,
        guardrails_fired=_merged_guardrails(physical),
        warnings=_merged_warnings(physical),
        freshness=[
            _freshness(sources_by_name[s])
            for s in sorted({s for p in physical for s in p.used_sources})
        ],
        finality=_merge_finality(physical),
        cte_count=cte_count,
        scoped_sources=frozenset(s for p in physical for s in p.scoped_sources),
        shared_sources=frozenset(s for p in physical for s in p.shared_sources),
    )


def _sortable(physical: _Physical) -> tuple[str, ...]:
    """Flatten a CTE's plan identity to a comparable tuple of strings."""
    key = physical.key_leaf.key
    return (
        key.source,
        key.strategy,
        "\x00".join(key.dimensions),
        "\x00".join(key.dimension_exprs),
        "\x00".join(key.filters),
        "\x00".join(f"{a}|{b}|{c}" for a, b, c in key.join_path),
        "\x00".join(key.finality),
        "\x00".join(f"{k}={v}" for k, v in key.strategy_params),
        "\x00".join(physical.columns),
    )


def _merged_guardrails(physical: Sequence[_Physical]) -> list[FiredGuardrail]:
    """Deduplicated union across every leaf, stable-sorted by ``(id, kind)`` (§7)."""
    seen: set[str] = set()
    merged: list[FiredGuardrail] = []
    for p in physical:
        for g in p.fired:
            if g.id not in seen:
                seen.add(g.id)
                merged.append(g)
    return sorted(merged, key=lambda g: (g.id, g.kind))


def _merged_warnings(physical: Sequence[_Physical]) -> list[str]:
    """Union in leaf-sort order, first occurrence wins (§7)."""
    merged: list[str] = []
    for p in physical:
        merged.extend(w for w in p.warnings if w not in merged)
    return merged
