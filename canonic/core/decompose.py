"""The ``decompose`` capability: attribute a metric's change between two periods.

It runs through the normal query path, so principal binding, tenancy, population filters,
guardrails, fanout handling and finality apply exactly as in ``query``. One statement fetches the
leaf metrics of the chosen method per period, and one more per split dimension. The attribution
itself is arithmetic on those aggregates, see :mod:`canonic.core.decompose_methods`.
"""

from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, cast

import sqlglot
from sqlglot import exp
from sqlglot.errors import ParseError

from canonic.compiler import SemanticQuery
from canonic.compiler._helpers import _dimension_output_names, _find_dimension
from canonic.compiler.joins import build_alias_tree
from canonic.compiler.leaf import _conjuncts, _render
from canonic.contract import CONTRACT_SCHEMA
from canonic.contracts.kinds import spec_for
from canonic.contracts.models import BindingKind
from canonic.core.decompose_methods import (
    AdditiveMethod,
    PeriodLabels,
    RatioPartsMethod,
    RatioShiftShareMethod,
)
from canonic.core.decompose_models import DecomposeResult
from canonic.core.models import (
    FinalityOut,
    FiredGuardrailOut,
    QueryMetadata,
    ScopeOut,
    SourceFreshnessOut,
    TrustScoreOut,
)
from canonic.core.query import _ANONYMOUS_PRINCIPAL, QueryRun, _event_user
from canonic.exc import (
    CanonicError,
    CapabilityDisabled,
    PeriodOpen,
    Unreachable,
    UnsupportedMeasure,
    ValidationFailed,
)
from canonic.instrumentation.models import AnswerEvent, _age_days, _sha256_json
from canonic.log import query_id_var
from canonic.trust.models import TrustTier
from canonic.trust.periods import floor_to_grain, parse_period_start, period_end

if TYPE_CHECKING:
    from collections.abc import Sequence

    from canonic.connectors.base import ResultSet
    from canonic.contracts.principal import Caller, Principal
    from canonic.contracts.resolver import Binding
    from canonic.core.context import ServiceContext
    from canonic.core.decompose_methods import DecompositionMethod, LeafValues, MemberValues
    from canonic.core.decompose_models import DecomposeRequest
    from canonic.core.models import CompositionOut
    from canonic.core.query import QueryService
    from canonic.semantic.models import Dimension

__all__ = ["DecomposeService", "merge_metadata"]

logger = logging.getLogger(__name__)

#: Why a kind cannot be attributed by member, named in ``UNSUPPORTED_MEASURE``.
_UNSUPPORTED: dict[BindingKind, str] = {
    BindingKind.SEMI_ADDITIVE: "it does not add up along its collapse dimension",
    BindingKind.DISTINCT_COUNT: "members overlap",
    BindingKind.PERCENTILE: "a quantile does not split into the members' quantiles",
    BindingKind.OPAQUE: "it is served only at its native grain",
    BindingKind.CUMULATIVE: "a running total carries every earlier period",
}

_TIER_ORDER = list(TrustTier)


@dataclass(frozen=True, slots=True)
class _Period:
    """The resolved period dimension and the two periods it is compared over."""

    dimension: str
    names: frozenset[str]
    grain: str
    #: When the periods begin. ``None`` for a label no known form reads, which leaves the
    #: open-period check of that period undone.
    base: datetime | None
    compare: datetime | None
    predicate: str
    labels: PeriodLabels
    #: Whether rows are matched to the periods by their label text (a ``time_grain``
    #: dimension) rather than by the period their date falls in.
    by_label: bool = False
    warnings: tuple[str, ...] = ()


class DecomposeService:
    """Attributes the change of one metric between two periods, overall and by member."""

    def __init__(self, ctx: ServiceContext, query: QueryService) -> None:
        self._ctx = ctx
        self._query = query

    async def decompose(
        self,
        request: DecomposeRequest,
        *,
        caller: Caller | None = None,
        principal: Principal | None = None,
    ) -> DecomposeResult:
        """Decompose the change of ``request.metric`` from ``period.base`` to ``period.compare``.

        Writes one answer event with ``capability: decompose`` for the whole call, also when it
        fails. Raises ``CAPABILITY_DISABLED`` when ``decompose.enabled`` is off.
        """
        started = time.perf_counter()
        runs: list[QueryRun] = []
        method: DecompositionMethod | None = None
        result: DecomposeResult | None = None
        error_code: str | None = None
        qid_token = query_id_var.set(uuid.uuid4().hex[:8])
        logger.info("decompose received: metric=%s by=%s", request.metric, request.by)
        try:
            self._check_enabled()
            binding = self._query.resolve_metric(
                request.metric, request.context, principal=principal
            )
            self._check_request(request)
            labels = PeriodLabels(
                request.period.dimension, str(request.period.base), str(request.period.compare)
            )
            method, warnings = self._method_for(binding, labels, split=bool(request.by))
            leaves = method.leaf_metrics()
            period = self._resolve_period(request, leaves, labels, principal)
            self._check_reachable(request, leaves, principal)
            _check_filters(request, period)
            filters = [*request.filters, period.predicate]
            if (population := binding.binding.canonical.population_filter) is not None:
                filters.append(population)

            overall_query = self._statement(request, leaves, [period.dimension], filters)
            overall_compiled = self._query.compile_query(overall_query, principal=principal)
            self._check_open(request, period, overall_compiled.finality is not None)

            runs.append(QueryRun())
            overall = await self._query.execute(
                overall_query, principal=principal, run=runs[-1], compiled=overall_compiled
            )
            (period_column,) = self._output_columns([period.dimension], leaves, principal)
            overall_values = _overall_values(overall.result, period, period_column, leaves)
            method.check(overall_values)

            top_n = self._top_n(request)
            breakdowns = {}
            metadata_blocks = [overall.metadata]
            for dimension in request.by:
                runs.append(QueryRun())
                split = await self._query.execute(
                    self._statement(request, leaves, [period.dimension, dimension], filters),
                    principal=principal,
                    run=runs[-1],
                )
                metadata_blocks.append(split.metadata)
                period_split_column, member_column = self._output_columns(
                    [period.dimension, dimension], leaves, principal
                )
                members = _member_values(
                    split.result, period, period_split_column, member_column, leaves
                )
                breakdowns[dimension] = method.split(overall_values, members, top_n)

            result = DecomposeResult(
                metric=binding.metric,
                kind=binding.kind.value,
                method=method.name,
                period=request.period,
                total=method.total(overall_values),
                effects=method.effects(overall_values),
                by=breakdowns,
                metadata=merge_metadata(
                    metadata_blocks,
                    composition=self._composition(binding, request, principal),
                    project_fingerprint=self._ctx.state.fingerprint,
                    warnings=[*warnings, *period.warnings],
                ),
            )
            return result
        except CanonicError as err:
            error_code = err.code.value if err.code is not None else None
            raise
        finally:
            latency_ms = round((time.perf_counter() - started) * 1000)
            self._emit_answer_event(
                request, method, runs, result, latency_ms, error_code, caller, principal
            )
            query_id_var.reset(qid_token)

    def _check_enabled(self) -> None:
        if not self._ctx.decompose_enabled:
            raise CapabilityDisabled(
                "the decompose capability is disabled; set decompose.enabled: true in "
                "canonic.yaml to use it"
            )

    def _check_request(self, request: DecomposeRequest) -> None:
        """The request-shape checks that need no metric or dimension resolved."""
        max_by = self._ctx.config.decompose.max_by
        if len(request.by) > max_by:
            raise ValidationFailed(
                f"decompose takes at most {max_by} split dimensions (decompose.max_by), "
                f"got {len(request.by)}"
            )
        duplicates = sorted({d for d in request.by if request.by.count(d) > 1})
        if duplicates:
            raise ValidationFailed(f"split dimensions {duplicates} are listed more than once")
        if request.period.dimension in request.by:
            raise ValidationFailed(
                f"the period dimension {request.period.dimension!r} cannot also be a split "
                f"dimension"
            )

    def _top_n(self, request: DecomposeRequest) -> int:
        config = self._ctx.config.decompose
        top_n = request.top_n if request.top_n is not None else config.top_n
        return min(top_n, config.max_top_n)

    def _method_for(
        self, binding: Binding, labels: PeriodLabels, *, split: bool
    ) -> tuple[DecompositionMethod, list[str]]:
        """The attribution method for ``binding``, and the warnings the choice carries.

        A ratio of two ``single`` metrics decomposes overall and by member. A ``distinct_count``
        component has exact overall effects, which need only the totals, but no split by member:
        one entity can count in several members, so the members' denominators do not sum to the
        total. ``split`` says whether the request asks for members.
        """
        kind = binding.kind
        if kind is BindingKind.SINGLE:
            return AdditiveMethod(binding.metric), []
        if kind in _UNSUPPORTED:
            raise UnsupportedMeasure(
                f"metric {binding.metric!r} cannot be decomposed: {kind.value} cannot be "
                f"attributed by member: {_UNSUPPORTED[kind]}"
            )
        components = binding.components
        assert components is not None  # noqa: S101 — ratio/weighted_avg always resolve components
        attrs = spec_for(kind).component_attrs
        assert attrs is not None  # noqa: S101 — composite kinds always name their components
        for label, component in zip(
            attrs, (components.numerator, components.denominator), strict=True
        ):
            if component.kind is BindingKind.DISTINCT_COUNT:
                if split:
                    raise UnsupportedMeasure(
                        f"metric {binding.metric!r} cannot be split by member: its {label} "
                        f"{component.metric!r} has kind distinct_count, and members overlap; "
                        f"decompose it without 'by' for the overall effects"
                    )
            elif component.kind is not BindingKind.SINGLE:
                raise UnsupportedMeasure(
                    f"metric {binding.metric!r} cannot be decomposed: its {label} "
                    f"{component.metric!r} has kind {component.kind.value}; a component must be "
                    f"of kind single, or distinct_count for the overall effects only"
                )
        numerator, denominator = components.numerator.metric, components.denominator.metric
        parts = binding.binding.canonical.numerator_parts
        if parts:
            tolerance = self._ctx.config.decompose.parts_tolerance
            return (
                RatioPartsMethod(binding.metric, numerator, denominator, parts, labels, tolerance),
                [],
            )
        warnings = []
        if (components.numerator.source, components.numerator.measure) == (
            components.denominator.source,
            components.denominator.measure,
        ) and not _narrows(components.numerator, components.denominator):
            warnings.append(
                f"components of {binding.metric} share source and measure; if the numerator "
                f"contains the denominator, declare numerator_parts"
            )
        return RatioShiftShareMethod(binding.metric, numerator, denominator, labels), warnings

    def _resolve_period(
        self,
        request: DecomposeRequest,
        leaves: Sequence[str],
        labels: PeriodLabels,
        principal: Principal | None,
    ) -> _Period:
        name = request.period.dimension
        alias, dimension = self._find(name, leaves[0], principal)
        grain = dimension.period_grain
        if grain is None:
            raise ValidationFailed(
                f"dimension {name!r} has no period grain; decompose needs a time dimension "
                f"with time_grain or a granularity of hour, day, week, month, quarter or year"
            )
        names = frozenset({dimension.name, *dimension.aliases})
        if dimension.time_grain is not None:
            return self._resolve_label_period(request, alias, dimension, grain, names, labels)
        starts = []
        for value in (request.period.base, request.period.compare):
            start = parse_period_start(value, grain)
            if start is None:
                raise ValidationFailed(f"{value!r} is not a {grain} of dimension {name!r}")
            starts.append(floor_to_grain(start, grain))
        base, compare = starts
        if base == compare:
            raise ValidationFailed(
                f"base {request.period.base!r} and compare {request.period.compare!r} are the "
                f"same {grain}; decompose compares two distinct periods"
            )
        return _Period(
            dimension=name,
            names=names,
            grain=grain,
            base=base,
            compare=compare,
            predicate=_period_predicate(alias, dimension, request, grain, base, compare),
            labels=labels,
        )

    def _resolve_label_period(
        self,
        request: DecomposeRequest,
        alias: str,
        dimension: Dimension,
        grain: str,
        names: frozenset[str],
        labels: PeriodLabels,
    ) -> _Period:
        """The two periods of a label dimension, which are filtered and matched by their text.

        The labels' starts only serve the open-period check. With a ``time_format`` a label
        that does not fit it is an error. Without one, a label no known form reads is still
        decomposed, with a warning that its period was not checked.
        """
        name = request.period.dimension
        if _label_key(request.period.base) == _label_key(request.period.compare):
            raise ValidationFailed(
                f"base {request.period.base!r} and compare {request.period.compare!r} are the "
                f"same {grain}; decompose compares two distinct periods"
            )
        time_format = dimension.period_format
        starts: list[datetime | None] = []
        warnings: list[str] = []
        for value in (request.period.base, request.period.compare):
            start = parse_period_start(value, grain, time_format)
            if start is None and time_format is not None:
                example = time_format.render(floor_to_grain(datetime.now(UTC), grain))
                raise ValidationFailed(
                    f"{value!r} does not fit the time_format {time_format.format!r} of "
                    f"dimension {name!r}, for example {example!r}"
                )
            if start is None:
                warnings.append(
                    f"{name}: {value!r} is not a {grain} label canonic can read, so whether "
                    f"its period has ended was not checked; declare time_format on the "
                    f"dimension"
                )
            starts.append(None if start is None else floor_to_grain(start, grain))
        base, compare = starts
        return _Period(
            dimension=name,
            names=names,
            grain=grain,
            base=base,
            compare=compare,
            predicate=_label_predicate(alias, dimension, request),
            labels=labels,
            by_label=True,
            warnings=tuple(warnings),
        )

    def _find(self, name: str, leaf: str, principal: Principal | None) -> tuple[str, Dimension]:
        """The alias and dimension ``name`` binds to from the source of leaf metric ``leaf``."""
        source = self._query.resolve_metric(leaf, principal=principal).source
        assert source is not None  # noqa: S101 — every leaf is a single metric
        sources_by_name = self._ctx.source_by_name
        found = _find_dimension(
            name, sources_by_name, source, build_alias_tree(source, sources_by_name)
        )
        if found is None:
            raise Unreachable(
                f"dimension {name!r} is not reachable from source {source!r} of leaf metric "
                f"{leaf!r}"
            )
        return found

    def _output_columns(
        self, names: Sequence[str], leaves: Sequence[str], principal: Principal | None
    ) -> list[str]:
        """The result columns the compiler names the dimensions ``names`` by, in order.

        A requested name may be an alias or qualified by its join alias (``pickup.city``), and
        the column is named by the dimension itself, qualified only when two of them share it.
        """
        return _dimension_output_names([self._find(name, leaves[0], principal) for name in names])

    def _check_reachable(
        self, request: DecomposeRequest, leaves: Sequence[str], principal: Principal | None
    ) -> None:
        for dimension in (request.period.dimension, *request.by):
            for leaf in leaves:
                self._find(dimension, leaf, principal)

    def _check_open(self, request: DecomposeRequest, period: _Period, has_finality: bool) -> None:
        """Refuse a period that has not ended, unless the request allows it.

        A finality rule governing the query takes precedence, as in ``query``: it marks the
        provisional rows itself. With ``trust.flag_open_periods`` off, nothing is checked.
        """
        now = self._query.serving_clock(request.as_of)
        if now is None or has_finality or request.allow_open_period:
            return
        now_utc = now.astimezone(UTC).replace(tzinfo=None) if now.tzinfo else now
        for start, label in (
            (period.base, period.labels.base),
            (period.compare, period.labels.compare),
        ):
            if start is not None and period_end(start, period.grain) > now_utc:
                raise PeriodOpen(
                    f"{period.dimension}: the {period.grain} {label} has not ended, so a change "
                    f"against it is misleading; set allow_open_period to decompose it anyway"
                )

    def _statement(
        self,
        request: DecomposeRequest,
        leaves: Sequence[str],
        dimensions: list[str],
        filters: list[str],
    ) -> SemanticQuery:
        return SemanticQuery(
            metrics=list(leaves),
            dimensions=dimensions,
            filters=filters,
            via=request.via,
            context=request.context,
            as_of=request.as_of,
        )

    def _composition(
        self, binding: Binding, request: DecomposeRequest, principal: Principal | None
    ) -> dict[str, CompositionOut]:
        """The ``composition`` block of the decomposed metric itself, compiled, not executed."""
        if binding.components is None:
            return {}
        compiled = self._query.compile_query(
            SemanticQuery(metrics=[binding.metric], context=request.context, as_of=request.as_of),
            principal=principal,
        )
        return QueryMetadata.from_compile_result(compiled).composition

    def _emit_answer_event(
        self,
        request: DecomposeRequest,
        method: DecompositionMethod | None,
        runs: list[QueryRun],
        result: DecomposeResult | None,
        latency_ms: int,
        error_code: str | None,
        caller: Caller | None,
        principal: Principal | None,
    ) -> None:
        try:
            effective_policy = self._ctx.resolver.authz_for(
                principal if principal is not None else _ANONYMOUS_PRINCIPAL
            )
            compiled = [run.compiled for run in runs if run.compiled is not None]
            results = [run.result for run in runs if run.result is not None]
            resolved: dict[str, str] = {}
            for compile_result in compiled:
                resolved.update(compile_result.resolved)
            freshness = {f.source: f for c in compiled for f in c.freshness}
            event = AnswerEvent(
                ts=datetime.now(UTC).isoformat(),
                contract_schema=CONTRACT_SCHEMA,
                capability="decompose",
                method=method.name.value if method is not None else None,
                query_hash=_sha256_json(request.model_dump(mode="json")),
                compiled_sql_hash=_sha256_json({"sql": [c.sql for c in compiled]})
                if compiled
                else None,
                connection=runs[0].connection_id if runs else None,
                resolved={"metrics": resolved} if resolved else {},
                guardrails_fired=sorted({g.id for c in compiled for g in c.guardrails_fired}),
                freshness=[
                    {
                        "age_days": _age_days(f.last_validated_at),
                        "source": f.source,
                        "stale": f.stale,
                    }
                    for _, f in sorted(freshness.items())
                ],
                latency_ms=latency_ms,
                bytes_scanned=_bytes_scanned(results),
                error=error_code,
                trust_score=result.metadata.trust_score.tier
                if result is not None and result.metadata.trust_score is not None
                else None,
                project_fingerprint=self._ctx.state.fingerprint,
                user=_event_user(caller),
                tenant=principal.tenant_scope if principal is not None else None,
                roles=list(effective_policy.roles) or None,
                tenancy_exempt=effective_policy.tenancy_exempt,
            )
            self._ctx.event_log.append(event)
        except Exception as exc:
            logger.warning("answer event emission failed: %s", exc)


def _check_filters(request: DecomposeRequest, period: _Period) -> None:
    """Reject a filter on the period dimension, which would contradict the two periods."""
    for predicate in request.filters:
        try:
            parsed = sqlglot.parse_one(predicate)
        except ParseError:
            continue  # the compiler reports the malformed filter with its own message
        if any(column.name in period.names for column in parsed.find_all(exp.Column)):
            raise ValidationFailed(
                f"filter {predicate!r} restricts the period dimension {period.dimension!r}; "
                f"decompose sets the two periods itself"
            )


def _narrows(numerator: Binding, denominator: Binding) -> bool:
    """Whether the numerator counts a strict subset of the denominator's rows.

    That holds when the numerator's ``population_filter`` ANDs every condition of the
    denominator's with at least one more. A share like this is what shift-share is for, so the
    hint to declare ``numerator_parts`` does not apply. Conditions compare in their rendered
    form, so a subset written differently is not recognized and keeps the hint.
    """
    narrower = _filter_conditions(numerator.binding.canonical.population_filter)
    wider = _filter_conditions(denominator.binding.canonical.population_filter)
    return narrower is not None and wider is not None and wider < narrower


def _filter_conditions(population_filter: str | None) -> frozenset[str] | None:
    """The conditions a ``population_filter`` ANDs together, or ``None`` when it does not parse."""
    if population_filter is None:
        return frozenset()
    try:
        condition = cast("exp.Expression", sqlglot.parse_one(population_filter))
    except ParseError:
        return None
    return frozenset(_render(conjunct) for conjunct in _conjuncts(condition))


def _bytes_scanned(results: Sequence[ResultSet]) -> int | None:
    scanned = [r.bytes_scanned for r in results if r.bytes_scanned is not None]
    return sum(scanned) if scanned else None


def _literal(value: str | int) -> str:
    if isinstance(value, int):
        return str(value)
    return "'" + value.replace("'", "''") + "'"


def _instant(moment: datetime) -> str:
    if moment.time() == datetime.min.time():
        return f"'{moment:%Y-%m-%d}'"
    return f"'{moment:%Y-%m-%d %H:%M:%S}'"


def _period_predicate(
    alias: str,
    dimension: Dimension,
    request: DecomposeRequest,
    grain: str,
    base: datetime,
    compare: datetime,
) -> str:
    """The filter that keeps the rows of the two periods of a bucketed time dimension.

    A filter on a dimension name binds to its column without bucketing, so it is filtered by
    range on its column. A derived dimension is filtered by its name, which the compiler
    inlines as its expression.
    """
    target = _target(alias, dimension)
    ranges = " OR ".join(
        f"({target} >= {_instant(start)} AND {target} < {_instant(period_end(start, grain))})"
        for start in (base, compare)
    )
    return f"({ranges})"


def _label_predicate(alias: str, dimension: Dimension, request: DecomposeRequest) -> str:
    """The filter that keeps the rows of the two periods of a label dimension, by their text."""
    values = ", ".join(_literal(v) for v in (request.period.base, request.period.compare))
    return f"{_target(alias, dimension)} IN ({values})"


def _target(alias: str, dimension: Dimension) -> str:
    return dimension.name if dimension.is_derived else f"{alias}.{dimension.column}"


def _period_index(value: Any, period: _Period) -> int | None:
    """0 for a row of the base period, 1 for the compare period, ``None`` for neither."""
    if period.by_label:
        key = _label_key(value)
        if key == _label_key(period.labels.base):
            return 0
        if key == _label_key(period.labels.compare):
            return 1
        return None
    start = parse_period_start(value, period.grain)
    if start is None:
        return None
    start = floor_to_grain(start, period.grain)
    if start == period.base:
        return 0
    if start == period.compare:
        return 1
    return None


def _label_key(value: Any) -> str | None:
    """A label as text, so ``2026`` from the request matches ``2026`` read from the warehouse."""
    return None if value is None else str(value).strip()


def _number(value: Any) -> float:
    return 0.0 if value is None else float(value)


def _accumulate(
    target: dict[str, list[float]],
    row: list[Any],
    columns: dict[str, int],
    leaves: Sequence[str],
    index: int,
) -> None:
    for leaf in leaves:
        target.setdefault(leaf, [0.0, 0.0])[index] += _number(row[columns[leaf]])


def _overall_values(
    result: ResultSet, period: _Period, period_column: str, leaves: Sequence[str]
) -> LeafValues:
    columns = {c.name: i for i, c in enumerate(result.columns)}
    values: dict[str, list[float]] = {leaf: [0.0, 0.0] for leaf in leaves}
    for row in result.rows:
        index = _period_index(row[columns[period_column]], period)
        if index is not None:
            _accumulate(values, row, columns, leaves, index)
    return {leaf: (pair[0], pair[1]) for leaf, pair in values.items()}


def _member_values(
    result: ResultSet,
    period: _Period,
    period_column: str,
    member_column: str,
    leaves: Sequence[str],
) -> MemberValues:
    columns = {c.name: i for i, c in enumerate(result.columns)}
    members: dict[Any, dict[str, list[float]]] = {}
    for row in result.rows:
        index = _period_index(row[columns[period_column]], period)
        if index is not None:
            _accumulate(
                members.setdefault(row[columns[member_column]], {}), row, columns, leaves, index
            )
    return {
        member: {leaf: (pair[0], pair[1]) for leaf, pair in values.items()}
        for member, values in members.items()
    }


def merge_metadata(
    blocks: Sequence[QueryMetadata],
    *,
    composition: dict[str, CompositionOut],
    project_fingerprint: str | None,
    warnings: Sequence[str] = (),
) -> QueryMetadata:
    """One metadata block for an answer served by several statements.

    Guardrails and warnings are the union, the trust tier is the worst one with the reasons at
    that tier, the finality watermark is the earliest one, and there is one freshness entry per
    source. ``composition`` and ``project_fingerprint`` describe the answer as a whole.
    """
    resolved: dict[str, str] = {}
    guardrails: dict[tuple[str, str], FiredGuardrailOut] = {}
    freshness: dict[str, SourceFreshnessOut] = {}
    merged_warnings: list[str] = list(dict.fromkeys(warnings))
    for block in blocks:
        resolved.update(block.resolved.get("metrics", {}))
        for guardrail in block.guardrails_fired:
            guardrails.setdefault((guardrail.id, guardrail.kind), guardrail)
        for entry in block.freshness:
            known = freshness.get(entry.source)
            if known is None or (entry.stale and not known.stale):
                freshness[entry.source] = entry
        merged_warnings.extend(w for w in block.warnings if w not in merged_warnings)
    return QueryMetadata(
        resolved={"metrics": resolved},
        guardrails_fired=[guardrails[key] for key in sorted(guardrails)],
        freshness=[freshness[source] for source in sorted(freshness)],
        warnings=merged_warnings,
        contract_schema=CONTRACT_SCHEMA,
        finality=_merged_finality(blocks),
        trust_score=_merged_trust(blocks),
        scope=_merged_scope(blocks),
        composition=composition,
        project_fingerprint=project_fingerprint,
    )


def _merged_trust(blocks: Sequence[QueryMetadata]) -> TrustScoreOut | None:
    scores = [b.trust_score for b in blocks if b.trust_score is not None]
    if not scores:
        return None
    worst = min((TrustTier(s.tier) for s in scores), key=_TIER_ORDER.index)
    reasons = [r for s in scores if s.tier == worst.value for r in s.reasons]
    return TrustScoreOut(tier=worst.value, reasons=list(dict.fromkeys(reasons)))


def _merged_finality(blocks: Sequence[QueryMetadata]) -> FinalityOut | None:
    finalities = [b.finality for b in blocks if b.finality is not None]
    if not finalities:
        return None
    final = [f.final_rows for f in finalities if f.final_rows is not None]
    provisional = [f.provisional_rows for f in finalities if f.provisional_rows is not None]
    return FinalityOut(
        watermark=min(f.watermark for f in finalities),
        sources_used=sorted({s for f in finalities for s in f.sources_used}),
        final_rows=sum(final) if final else None,
        provisional_rows=sum(provisional) if provisional else None,
    )


def _merged_scope(blocks: Sequence[QueryMetadata]) -> ScopeOut | None:
    scopes = [b.scope for b in blocks if b.scope is not None]
    if not scopes:
        return None
    first = scopes[0]
    return ScopeOut(
        tenant=first.tenant,
        scoped_sources=list(dict.fromkeys(s for scope in scopes for s in scope.scoped_sources)),
        shared_sources=list(dict.fromkeys(s for scope in scopes for s in scope.shared_sources)),
        roles=first.roles,
        tenancy_exempt=first.tenancy_exempt,
    )
