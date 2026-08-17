"""Simple/additive compile path (stages 2-8, the default route, SPEC §4)."""

from __future__ import annotations

import logging
from datetime import datetime
from typing import TYPE_CHECKING

from sqlglot import exp

from canonic.compiler.compose import LeafRef, MetricLeaves, MetricPlan
from canonic.compiler.leaf import LeafContext, LeafMetric, plan_leaf
from canonic.exc import Unresolved

if TYPE_CHECKING:
    from canonic.compiler.query import SemanticQuery
    from canonic.contracts.models import FinalityRule
    from canonic.contracts.principal import EffectivePolicy, Principal
    from canonic.contracts.resolver import Binding as ResolverBinding
    from canonic.contracts.resolver import ContractResolver
    from canonic.semantic.models import SemanticSource

from canonic.compiler._helpers import (
    _TIME_TYPES,
    _block_or_warn,
    _find_measure,
    _parse,
    _ResolvedMetric,
)

logger = logging.getLogger(__name__)


def _bind_metric(
    name: str,
    binding: ResolverBinding,
    sources_by_name: dict[str, SemanticSource],
) -> _ResolvedMetric:
    """Bind a resolved single-kind binding to its source and measure objects (stage 1)."""
    assert binding.source is not None and binding.measure is not None  # noqa: S101
    source = sources_by_name.get(binding.source)
    if source is None:
        raise Unresolved(f"metric {name!r} binds to unknown source {binding.source!r}")
    measure = _find_measure(source, binding.measure)
    if measure is None:
        raise Unresolved(
            f"metric {name!r} binds to unknown measure {binding.source}.{binding.measure!r}"
        )
    return _ResolvedMetric(name=name, source=binding.source, measure=measure)


def plan_metric(
    query: SemanticQuery,
    queried_name: str,
    binding: ResolverBinding,
    resolver: ContractResolver,
    sources_by_name: dict[str, SemanticSource],
    *,
    principal: Principal,
    effective_policy: EffectivePolicy,
) -> MetricLeaves:
    """Plan a plain additive metric as one leaf aggregating to the requested dimensions.

    One leaf per metric, never one per source: a leaf is defined by its *plan*, and two
    metrics on one source whose populations or guardrails differ are not the same plan.
    Compose fuses back together whatever genuinely is identical, so nothing is lost by
    starting from the finest split — and the alternative, sharing a SELECT between metrics
    with different filters, is what conditional aggregation used to paper over.
    """
    resolved = _bind_metric(queried_name, binding, sources_by_name)
    leaf_metric = LeafMetric(
        resolved=resolved, population_filter=binding.binding.canonical.population_filter
    )
    leaf = plan_leaf(
        LeafContext(
            query=query,
            resolver=resolver,
            sources_by_name=sources_by_name,
            principal=principal,
            effective_policy=effective_policy,
        ),
        resolved.source,
        [leaf_metric],
        finality_metric=queried_name,
    )
    # The output column keeps the measure's name, as it always has on this path.
    column = resolved.measure.name
    return MetricLeaves(
        leaves=[leaf],
        metric=MetricPlan(name=column, refs=(LeafRef(leaf=0, column=column),)),
        resolved=f"{resolved.source}.{column}",
    )


def _parse_datetime_literal(lit: str) -> datetime | None:
    """Try to parse an ISO date or datetime literal; return None if unparseable."""
    from datetime import UTC

    for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%d %H:%M:%S%z", "%Y-%m-%d"):
        try:
            dt = datetime.strptime(lit.strip("'\""), fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=UTC)
            return dt
        except ValueError:
            continue
    return None


def _time_column_names(
    finality_rule: FinalityRule,
    sources_by_name: dict[str, SemanticSource],
) -> frozenset[str]:
    """Return the set of physical column names that back the time dimension on any realization."""
    names: set[str] = set()
    for realization in finality_rule.realizations:
        source = sources_by_name.get(realization.source)
        if source is None:
            continue
        for dim in source.dimensions:
            col = next((c for c in source.columns if c.name == dim.column), None)
            if col is not None and col.type in _TIME_TYPES:
                names.add(dim.column)
                names.add(dim.name)
    return frozenset(names)


def _window_exceeds_watermark(
    filters: list[str],
    time_names: frozenset[str],
    watermark_dt: datetime,
    sources_by_name: dict[str, SemanticSource],
) -> bool:
    """Return True if the query window, as derived from time-dimension filters, exceeds watermark.

    Decision rule (per spec §2.4, confirmed):
    - No time predicate at all → False (allow; coalescing handles per-row finality).
    - Finite upper bound U → block iff U > watermark.
    - Open upper bound but finite lower bound L → block iff L > watermark.
    """
    upper: datetime | None = None  # minimum upper-bound literal found
    lower: datetime | None = None  # maximum lower-bound literal found
    found_any = False

    for raw in filters:
        try:
            parsed = _parse(raw)
        except Exception:  # noqa: BLE001
            continue
        for node in parsed.walk():
            # Determine if this comparison node touches a time column.
            if isinstance(node, (exp.LTE, exp.LT, exp.GTE, exp.GT, exp.EQ)):
                col_node = node.this if isinstance(node.this, exp.Column) else None
                lit_node = node.expression if isinstance(node.expression, exp.Literal) else None
                # Also handle reversed comparisons: literal op column
                if col_node is None and isinstance(node.this, exp.Literal):
                    lit_node = node.this
                    col_node = node.expression if isinstance(node.expression, exp.Column) else None
                if col_node is None or lit_node is None:
                    continue
                col_name = col_node.name
                if col_name not in time_names:
                    continue
                dt = _parse_datetime_literal(lit_node.this)
                if dt is None:
                    continue
                found_any = True
                if isinstance(node, (exp.LTE, exp.LT)):
                    upper = dt if upper is None else min(upper, dt)
                elif isinstance(node, (exp.GTE, exp.GT)):
                    lower = dt if lower is None else max(lower, dt)
                else:  # EQ
                    upper = dt if upper is None else min(upper, dt)
                    lower = dt if lower is None else max(lower, dt)
            elif isinstance(node, exp.Between):
                col_node = node.this if isinstance(node.this, exp.Column) else None
                if col_node is None or col_node.name not in time_names:
                    continue
                lo_node = node.args.get("low")
                hi_node = node.args.get("high")
                lo = (
                    _parse_datetime_literal(lo_node.this)
                    if isinstance(lo_node, exp.Literal)
                    else None
                )
                hi = (
                    _parse_datetime_literal(hi_node.this)
                    if isinstance(hi_node, exp.Literal)
                    else None
                )
                if lo is not None:
                    found_any = True
                    lower = lo if lower is None else max(lower, lo)
                if hi is not None:
                    found_any = True
                    upper = hi if upper is None else min(upper, hi)

    if not found_any:
        return False

    from datetime import UTC

    wm = watermark_dt
    if upper is not None:
        upper_utc = upper.astimezone(UTC)
        wm_utc = wm.astimezone(UTC)
        return upper_utc > wm_utc
    if lower is not None:
        lower_utc = lower.astimezone(UTC)
        wm_utc = wm.astimezone(UTC)
        return lower_utc > wm_utc
    return False


def _enforce_restrict_source(
    query: SemanticQuery,
    metrics: list[_ResolvedMetric],
    resolver: ContractResolver,
    finality_rule: FinalityRule | None,
    sources_by_name: dict[str, SemanticSource],
) -> list[str]:
    """Stage 5b: block or warn when a restrict_source guardrail is violated (SPEC §2.4).

    ``severity: error`` (the default) raises GuardrailBlock; ``severity: warn`` returns a
    warning line instead of blocking the query.
    """
    warnings: list[str] = []
    if not query.context:
        return warnings

    seen: set[str] = set()
    for m in metrics:
        for guardrail in resolver.restrict_source_for(m.source, m.measure.name, query.context):
            if guardrail.restrict_to is None or guardrail.restrict_to.role != "final":
                continue
            rule = finality_rule if finality_rule is not None else resolver.finality_for(m.name)
            if rule is None:
                continue
            final_r = next((r for r in rule.realizations if r.role == "final"), None)
            if final_r is None or not final_r.watermark or not final_r.tz:
                continue

            from canonic.contracts.finality import evaluate_watermark

            watermark_dt = evaluate_watermark(final_r.watermark, final_r.tz, query.as_of)
            time_names = _time_column_names(rule, sources_by_name)
            if guardrail.id not in seen and _window_exceeds_watermark(
                query.filters, time_names, watermark_dt, sources_by_name
            ):
                logger.warning(
                    "restrict_source enforced: guardrail=%s watermark exceeded", guardrail.id
                )
                seen.add(guardrail.id)
                warnings.append(_block_or_warn(guardrail))
    return warnings
