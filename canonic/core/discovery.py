"""Discovery capabilities: list, describe, and group metrics for exploration (SPEC §4.1)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from canonic.compiler.joins import build_alias_tree, reachable_dimension_names
from canonic.compiler.result import TrustInput
from canonic.contracts.kinds import spec_for
from canonic.contracts.models import Status
from canonic.core.models import (
    DimensionInfo,
    DomainGroup,
    MetricDetail,
    MetricRef,
    MetricSummary,
    OverviewResult,
    SourceFreshnessOut,
)
from canonic.core.overview import questions_for_group
from canonic.exc import CanonicError, Unresolved, UnsupportedMeasure
from canonic.trust.scorer import TrustScorer
from canonic.trust.signals import static_signals_for

if TYPE_CHECKING:
    from canonic.contracts.models import MetricBinding
    from canonic.contracts.resolver import ContractResolver
    from canonic.core.context import ServiceContext
    from canonic.semantic.models import Dimension as _Dimension
    from canonic.trust.models import TrustScore


def _get_domain(binding: MetricBinding, resolver: ContractResolver) -> str:
    """Return the domain (owning source name) for *binding*.

    For source-bound kinds the canonical source is the domain.
    For composite kinds (ratio/weighted_avg) we walk the numerator's resolved binding.
    Falls back to the metric name when nothing resolves.
    """
    ref = binding.canonical
    spec = spec_for(ref.kind)
    metric: str = str(binding.metric)
    if spec.is_source_bound:
        return ref.source if ref.source is not None else metric
    # Composite kinds: walk the first component (numerator / weighted_sum) to its source.
    num = spec.component_names(ref)[0]
    if num is None:
        return metric
    candidates = resolver.bindings_for(num)
    if candidates:
        src: str | None = candidates[0].canonical.source
        if src is not None:
            return src
    return metric


class DiscoveryService:
    """Read-only metric discovery: summaries, detail, trust worklist, and domain overview."""

    def __init__(self, ctx: ServiceContext) -> None:
        self._ctx = ctx

    def list_metrics(self) -> list[MetricSummary]:
        """Return a summary of every active canonical metric (SPEC §4.1)."""
        summaries: list[MetricSummary] = []
        for b in self._ctx.resolver.active_bindings():
            canonical = b.canonical
            spec = spec_for(canonical.kind)
            source: str | None
            measure: str | None
            components: list[str] | None
            if spec.is_describable:
                source, measure, components = canonical.source, spec.column_field(canonical), None
            elif spec.is_composite:
                source, measure = None, None
                first, second = spec.component_names(canonical)
                assert first is not None and second is not None  # noqa: S101 — model_validator
                components = [first, second]
            else:
                continue  # OPAQUE and future kinds not surfaced in summary
            summaries.append(
                MetricSummary(
                    metric=b.metric,
                    kind=canonical.kind.value,
                    source=source,
                    measure=measure,
                    status=b.status.value,
                    aliases=list(b.aliases),
                    components=components,
                )
            )
        # deduplicate by metric name (each metric may appear multiple times in the name index
        # via aliases) and sort for determinism
        seen: set[str] = set()
        deduped: list[MetricSummary] = []
        for s in sorted(summaries, key=lambda x: x.metric):
            if s.metric not in seen:
                seen.add(s.metric)
                deduped.append(s)
        # enrich each summary with its queryable dimensions
        enriched: list[MetricSummary] = []
        for s in deduped:
            try:
                detail = self.describe_metric(s.metric)
                enriched.append(s.model_copy(update={"dimensions": detail.dimensions}))
            except CanonicError:
                enriched.append(s)
        return enriched

    def trust_report(self) -> list[tuple[str, TrustScore]]:
        """Static trust tier for every active canonical metric, sorted by name.

        A worklist for improving context (SPEC-E14 §8): uses only the static,
        compile-time signals (provenance, assertion coverage) — the same ones a
        ``min_trust`` guardrail would enforce. A served query may still score lower than
        this: E11's dynamic outcome-history signal (SPEC-E11 §5) can additionally cap a
        binding at ``caution`` at serve time — see ``canonic report``'s Feedback loop
        section for that per-binding state.
        """
        seen: set[str] = set()
        scores: list[tuple[str, TrustScore]] = []
        for b in self._ctx.resolver.active_bindings():
            if b.metric in seen:
                continue
            seen.add(b.metric)
            trust_input = TrustInput(
                metric=b.metric,
                provenance=b.provenance.value,
                has_assertion=bool(self._ctx.resolver.assertions_for({"metrics": [b.metric]})),
            )
            scores.append((b.metric, TrustScorer.score(static_signals_for([trust_input]))))
        return sorted(scores, key=lambda item: item[0])

    def describe_metric(self, name: str) -> MetricDetail:
        """Return grain, dimensions, measures, and freshness for a metric (SPEC §4.1).

        ``dimensions`` includes every dimension queryable against this metric — both those
        declared on the owning source and those reachable via its declared join graph.  The
        compiler resolves dimensions globally across sources (SPEC §4 stage 2), so this list
        accurately reflects what can be passed as a dimension in a ``query()`` call.

        Raises :class:`canonic.exc.Unresolved` or :class:`canonic.exc.Ambiguous` when the
        name does not resolve to exactly one active binding.
        """
        binding = self._ctx.resolve_or_raise(name)
        spec = spec_for(binding.kind)
        if spec.is_composite:
            assert binding.components is not None  # noqa: S101
            all_dims: list[DimensionInfo] = []
            all_measures: list[str] = []
            seen_dim_names: set[str] = set()
            for component in (binding.components.numerator, binding.components.denominator):
                if component.source is None:
                    continue
                for d in self._reachable_dimensions(component.source):
                    if d.name not in seen_dim_names:
                        seen_dim_names.add(d.name)
                        all_dims.append(d)
                comp_src = self._ctx.source_by_name.get(component.source)
                if comp_src is not None:
                    for m in comp_src.measures:
                        if m.name not in all_measures:
                            all_measures.append(m.name)
            return MetricDetail(
                metric=binding.metric,
                source=None,
                measure=None,
                grain=[],
                dimensions=all_dims,
                measures=all_measures,
                aliases=list(binding.binding.aliases),
                freshness=None,
                examples=list(binding.binding.examples),
            )
        if not spec.is_describable:
            raise UnsupportedMeasure(
                f"metric {name!r} is a {binding.kind} metric — "
                "use query() to compute it; describe_metric() requires a source-based metric"
            )
        assert binding.source is not None  # noqa: S101 — all describable kinds have source
        source = self._ctx.source_by_name.get(binding.source)
        if source is None:
            raise Unresolved(
                f"metric {name!r} resolved to source {binding.source!r} but that source"
                " is not loaded — check semantics/"
            )
        freshness: SourceFreshnessOut | None = None
        if source.meta.last_validated_at is not None:
            freshness = SourceFreshnessOut(
                source=source.name,
                last_validated_at=source.meta.last_validated_at.isoformat(),
                stale=False,
            )
        return MetricDetail(
            metric=binding.metric,
            source=binding.source,
            measure=binding.measure,
            grain=list(source.grain),
            dimensions=self._reachable_dimensions(source.name),
            measures=[m.name for m in source.measures],
            aliases=list(binding.binding.aliases),
            freshness=freshness,
            examples=list(binding.binding.examples),
        )

    def get_overview(self, domain: str | None = None) -> OverviewResult:
        """Return active metrics grouped by domain with plain-language sample questions (S12).

        ``domain`` filters to a single owning-source group; omit for all domains.
        Each group carries the source's reachable dimension names and ≥1 sample question
        (templated from binding examples or from dimensions when no usage evidence exists).
        """
        source_to_metrics: dict[str, list[tuple[str, str]]] = {}
        for b in self._ctx.resolver.active_bindings():
            d = _get_domain(b, self._ctx.resolver)
            source_to_metrics.setdefault(d, [])
            if any(n == b.metric for n, _ in source_to_metrics[d]):
                continue
            display: str = b.label or b.metric.replace("_", " ")
            source_to_metrics[d].append((b.metric, display))

        groups: list[DomainGroup] = []
        for src_name in sorted(source_to_metrics):
            if domain is not None and src_name != domain:
                continue
            name_label_pairs = sorted(source_to_metrics[src_name], key=lambda x: x[0])
            metric_refs = [MetricRef(name=n, label=lbl) for n, lbl in name_label_pairs]
            dim_names = [d.name for d in self._reachable_dimensions(src_name)]
            metrics_with_examples: list[tuple[str, list[Any]]] = []
            for name, label in name_label_pairs:
                bindings = self._ctx.resolver.bindings_for(name)
                examples: list[Any] = []
                for b in bindings:
                    if b.metric == name and b.status is Status.ACTIVE:
                        examples = list(b.examples)
                        break
                metrics_with_examples.append((label, examples))
            groups.append(
                DomainGroup(
                    name=src_name,
                    metrics=metric_refs,
                    dimensions=dim_names,
                    sample_questions=questions_for_group(metrics_with_examples, dim_names),
                )
            )
        return OverviewResult(domains=groups)

    def _reachable_dimensions(self, source_name: str) -> list[DimensionInfo]:
        """All dimensions queryable from *source_name* via its declared join graph.

        Traverses the join graph breadth-first using aliases. Dimensions reachable under
        only one alias are returned with an unqualified ``name``; dimensions reachable
        under multiple aliases (e.g. ``city`` via both ``pickup`` and ``dropoff``) are
        returned qualified (``pickup.city``, ``dropoff.city``) so the caller always gets
        a usable name to pass to ``query()``.
        """
        alias_to_source = build_alias_tree(source_name, self._ctx.source_by_name)
        dim_lookup: dict[tuple[str, str], _Dimension] = {
            (alias_to_source.get(alias, alias), d.name): d
            for alias in alias_to_source
            for src in [self._ctx.source_by_name.get(alias_to_source.get(alias, alias))]
            if src is not None
            for d in src.dimensions
        }

        result: list[DimensionInfo] = []
        for entry_name, alias in reachable_dimension_names(source_name, self._ctx.source_by_name):
            src_name = alias_to_source.get(alias, alias)
            dim = dim_lookup.get((src_name, entry_name.split(".")[-1]))
            result.append(
                DimensionInfo(
                    name=entry_name,
                    source=alias,
                    label=dim.label if dim else None,
                    description=dim.description if dim else None,
                )
            )
        return result
