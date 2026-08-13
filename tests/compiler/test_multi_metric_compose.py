"""Multi-metric compose — acceptance criteria S11-S17 (AMENDMENT-multi-metric-compose §8).

Shape and metadata assertions. The claim that the *numbers* are right lives next door in
`test_multi_metric_equivalence.py`, which executes each metric both ways and compares:
these tests say the compiler emitted the plan it meant to, that one says the plan was
correct. Both are needed — a shape assertion cannot catch a wrong join, and an execution
test cannot catch a leaf that was silently emitted twice.
"""

from __future__ import annotations

import duckdb
import pytest
import sqlglot

from canonic import exc
from canonic.compiler import SemanticQuery, compile
from canonic.contracts.models import (
    AppliesTo,
    BindingKind,
    CanonicalRef,
    Guardrail,
    GuardrailKind,
    MetricBinding,
    Provenance,
    Severity,
)
from canonic.contracts.resolver import ContractResolver
from canonic.semantic.models import Column, Dimension, Join, Measure, Relationship, SemanticSource

_DIALECTS = {"w": "duckdb"}


def _parse_ok(sql: str) -> None:
    sqlglot.parse_one(sql, dialect="postgres")


def _cte_names(sql: str) -> list[str]:
    """Every CTE name in the emitted statement, in declaration order."""
    return [cte.alias for cte in sqlglot.parse_one(sql, dialect="postgres").ctes]


@pytest.fixture
def orders() -> SemanticSource:
    return SemanticSource(
        name="orders",
        connection="w",
        table="fct_orders",
        grain=["order_id"],
        columns=[
            Column(name="order_id", type="string", nullable=False),
            Column(name="customer_id", type="string", nullable=False),
            Column(name="amount", type="decimal", nullable=True),
            Column(name="status", type="string", nullable=False),
        ],
        measures=[
            Measure(name="revenue", expr="sum(amount)", additivity="additive"),
            Measure(name="order_count", expr="count(*)", additivity="additive"),
        ],
        dimensions=[Dimension(name="status", column="status")],
        joins=[
            Join(
                to="customers",
                on="orders.customer_id = customers.customer_id",
                relationship=Relationship.MANY_TO_ONE,
            )
        ],
    )


@pytest.fixture
def customers() -> SemanticSource:
    return SemanticSource(
        name="customers",
        connection="w",
        table="dim_customers",
        grain=["customer_id"],
        columns=[
            Column(name="customer_id", type="string", nullable=False),
            Column(name="region", type="string", nullable=False),
        ],
        dimensions=[Dimension(name="region", column="region")],
    )


@pytest.fixture
def shipments() -> SemanticSource:
    """A second fact at a different native grain, reaching region by its own route."""
    return SemanticSource(
        name="shipments",
        connection="w",
        table="fct_shipments",
        grain=["shipment_id"],
        columns=[
            Column(name="shipment_id", type="string", nullable=False),
            Column(name="customer_id", type="string", nullable=False),
            Column(name="weight", type="decimal", nullable=True),
        ],
        measures=[Measure(name="shipped_weight", expr="sum(weight)", additivity="additive")],
        dimensions=[],
        joins=[
            Join(
                to="customers",
                on="shipments.customer_id = customers.customer_id",
                relationship=Relationship.MANY_TO_ONE,
            )
        ],
    )


@pytest.fixture
def sources(
    orders: SemanticSource, customers: SemanticSource, shipments: SemanticSource
) -> list[SemanticSource]:
    return [orders, customers, shipments]


@pytest.fixture
def bindings() -> list[MetricBinding]:
    """Metrics spanning every relationship compose has to get right.

    ``avg_order_value`` and ``weight_per_order`` are two ratios sharing the *same*
    denominator, which is how S12 AC2 checks that the shared leaf is emitted once.
    """
    single = [
        MetricBinding(metric="revenue", canonical=CanonicalRef(source="orders", measure="revenue")),
        MetricBinding(
            metric="order_count", canonical=CanonicalRef(source="orders", measure="order_count")
        ),
        MetricBinding(
            metric="shipped_weight",
            canonical=CanonicalRef(source="shipments", measure="shipped_weight"),
        ),
    ]
    ratios = [
        MetricBinding(
            metric="avg_order_value",
            canonical=CanonicalRef(
                kind=BindingKind.RATIO, numerator="revenue", denominator="order_count"
            ),
        ),
        MetricBinding(
            metric="weight_per_order",
            canonical=CanonicalRef(
                kind=BindingKind.RATIO, numerator="shipped_weight", denominator="order_count"
            ),
        ),
    ]
    return [*single, *ratios]


@pytest.fixture
def resolver(bindings: list[MetricBinding]) -> ContractResolver:
    return ContractResolver(bindings=bindings, guardrails=[])


@pytest.fixture
def guarded_resolver(bindings: list[MetricBinding]) -> ContractResolver:
    """``order_count`` carries a mandatory_filter that ``revenue`` does not."""
    return ContractResolver(
        bindings=bindings,
        guardrails=[
            Guardrail(
                id="orders-exclude-refunds",
                applies_to=AppliesTo(source="orders", measure="order_count"),
                kind=GuardrailKind.MANDATORY_FILTER,
                filter="status != 'refunded'",
                severity=Severity.ERROR,
                rationale="A refunded order is a reversal, not an order.",
            )
        ],
    )


# ---------------------------------------------------------------------------
# S11 — several metrics from one source compile to one query
# ---------------------------------------------------------------------------


def test_s11_ac1_metrics_sharing_a_plan_compile_to_one_statement(
    resolver: ContractResolver, sources: list[SemanticSource]
) -> None:
    """One statement, columns in request order.

    The amendment's S11 AC1 says "one CTE per metric". This implementation emits one CTE
    per distinct leaf *plan* instead: two metrics on one source with the same filters are
    the same query, and splitting them would scan the table twice to no purpose. Here they
    fuse all the way down to a single flat SELECT with no CTE at all.
    """
    result = compile(
        SemanticQuery(metrics=["revenue", "order_count"], dimensions=["region"]), resolver, sources
    )
    _parse_ok(result.sql)
    assert _cte_names(result.sql) == []
    assert result.sql.upper().count("SELECT") == 1
    projections = sqlglot.parse_one(result.sql, dialect="postgres").expressions
    assert [p.alias for p in projections] == ["region", "revenue", "order_count"]


def test_s11_ac1_differing_plans_get_one_cte_each_named_by_sort_order(
    guarded_resolver: ContractResolver, sources: list[SemanticSource]
) -> None:
    """Metrics whose effective filters differ get a leaf each, plus the grain spine."""
    result = compile(
        SemanticQuery(metrics=["revenue", "order_count"], dimensions=["region"]),
        guarded_resolver,
        sources,
    )
    _parse_ok(result.sql)
    assert _cte_names(result.sql) == ["_leaf_0", "_leaf_1", "_grain"]
    assert result.sql.upper().count("WHERE") == 1, "only the guarded leaf is filtered"


def test_s11_ac2_guardrails_fired_is_a_deduplicated_sorted_union(
    guarded_resolver: ContractResolver, sources: list[SemanticSource]
) -> None:
    """Each metric's guardrails fire on its own leaf; the result reports their union (§7)."""
    result = compile(
        SemanticQuery(metrics=["revenue", "order_count", "avg_order_value"]),
        guarded_resolver,
        sources,
    )
    fired = [(g.id, g.kind) for g in result.guardrails_fired]
    assert fired == sorted(fired), "stable-sorted by (id, kind)"
    assert [g.id for g in result.guardrails_fired] == ["orders-exclude-refunds"]


# ---------------------------------------------------------------------------
# S12 — ratios combine with non-ratios
# ---------------------------------------------------------------------------


def test_s12_ac1_ratio_alongside_a_scalar_metric(
    resolver: ContractResolver, sources: list[SemanticSource]
) -> None:
    """The request that used to fail outright: a ratio next to another metric."""
    result = compile(
        SemanticQuery(metrics=["revenue", "avg_order_value"], dimensions=["region"]),
        resolver,
        sources,
    )
    _parse_ok(result.sql)
    projections = sqlglot.parse_one(result.sql, dialect="postgres").expressions
    assert [p.alias for p in projections] == ["region", "revenue", "avg_order_value"]
    assert "NULLIF" in result.sql.upper(), "the division is applied after aggregation"


def test_s12_ac2_two_ratios_sharing_a_denominator_emit_it_once(
    resolver: ContractResolver, sources: list[SemanticSource]
) -> None:
    """Four component leaves, three distinct plans, and ``order_count`` scanned once.

    ``avg_order_value`` is revenue/order_count and ``weight_per_order`` is
    shipped_weight/order_count. revenue and order_count share a plan and fuse; the
    shipments leaf is its own; the second order_count leaf deduplicates against the first.
    """
    result = compile(
        SemanticQuery(metrics=["avg_order_value", "weight_per_order"], dimensions=["region"]),
        resolver,
        sources,
    )
    _parse_ok(result.sql)
    assert _cte_names(result.sql) == ["_leaf_0", "_leaf_1", "_grain"]
    assert result.sql.upper().count("COUNT(") == 1, "the shared denominator is aggregated once"


def test_s12_ac3_disabling_dedup_changes_the_sql_but_not_the_numbers(
    resolver: ContractResolver, sources: list[SemanticSource]
) -> None:
    """Leaf dedup is an optimisation: identical numbers with it on and off.

    This is the falsifiable half of the dedup key. A key that is subtly too loose merges
    leaves whose rows genuinely differ, and the result still looks plausible — so the
    property worth testing is not "dedup happened" but "dedup changed nothing that matters".
    """
    query = SemanticQuery(metrics=["avg_order_value", "weight_per_order"], dimensions=["region"])
    deduped = compile(query, resolver, sources, connection_dialects=_DIALECTS)
    verbose = compile(query, resolver, sources, connection_dialects=_DIALECTS, _dedup_leaves=False)

    assert len(_cte_names(verbose.sql)) > len(_cte_names(deduped.sql))

    con = duckdb.connect(":memory:")
    con.execute(
        "CREATE TABLE fct_orders (order_id VARCHAR, customer_id VARCHAR, "
        "amount DECIMAL(10,2), status VARCHAR)"
    )
    con.execute("CREATE TABLE dim_customers (customer_id VARCHAR, region VARCHAR)")
    con.execute(
        "CREATE TABLE fct_shipments (shipment_id VARCHAR, customer_id VARCHAR, "
        "weight DECIMAL(10,2))"
    )
    con.execute("INSERT INTO fct_orders VALUES ('1','c1',100,'paid'),('2','c2',50,'paid')")
    con.execute("INSERT INTO dim_customers VALUES ('c1','north'),('c2','south')")
    con.execute("INSERT INTO fct_shipments VALUES ('s1','c1',7),('s2','c2',3)")

    assert sorted(con.execute(deduped.sql).fetchall()) == sorted(
        con.execute(verbose.sql).fetchall()
    )


def test_s12_ac3_a_component_with_its_own_population_does_not_dedup(
    sources: list[SemanticSource],
) -> None:
    """The negative half: a differing population must keep the leaves apart.

    ``paid_revenue`` restricts the population ``revenue`` does not, so the two are not the
    same query however similar they look. A dedup key that ignored ``population_filter``
    would collapse them and report one number twice.
    """
    resolver = ContractResolver(
        bindings=[
            MetricBinding(
                metric="revenue", canonical=CanonicalRef(source="orders", measure="revenue")
            ),
            MetricBinding(
                metric="paid_revenue",
                canonical=CanonicalRef(
                    source="orders", measure="revenue", population_filter="status = 'paid'"
                ),
            ),
        ],
        guardrails=[],
    )
    result = compile(SemanticQuery(metrics=["revenue", "paid_revenue"]), resolver, sources)
    _parse_ok(result.sql)
    assert _cte_names(result.sql) == ["_leaf_0", "_leaf_1"]
    assert result.sql.upper().count("SUM(") == 2, "two populations, two aggregates"


# ---------------------------------------------------------------------------
# S13 — differing native grains do not block combination
# ---------------------------------------------------------------------------


def test_s13_ac1_different_native_grains_combine_at_the_requested_grain(
    resolver: ContractResolver, sources: list[SemanticSource]
) -> None:
    """Per-order and per-shipment metrics at region grain: no reconciliation needed.

    Neither CTE emits anything finer than region, so the native grain of either source is
    irrelevant to whether they can be combined (AMENDMENT §2).
    """
    result = compile(
        SemanticQuery(metrics=["revenue", "shipped_weight"], dimensions=["region"]),
        resolver,
        sources,
    )
    _parse_ok(result.sql)
    assert _cte_names(result.sql) == ["_leaf_0", "_leaf_1", "_grain"]
    assert result.sql.upper().count("GROUP BY") == 2, "each leaf aggregates to region itself"


def test_s13_ac2_a_dimension_value_missing_from_one_leaf_reads_null(
    resolver: ContractResolver, sources: list[SemanticSource]
) -> None:
    """Absent is NULL, not 0, and the row is not dropped (§4.2)."""
    result = compile(
        SemanticQuery(metrics=["revenue", "shipped_weight"], dimensions=["region"]),
        resolver,
        sources,
        connection_dialects=_DIALECTS,
    )
    con = duckdb.connect(":memory:")
    con.execute(
        "CREATE TABLE fct_orders (order_id VARCHAR, customer_id VARCHAR, "
        "amount DECIMAL(10,2), status VARCHAR)"
    )
    con.execute("CREATE TABLE dim_customers (customer_id VARCHAR, region VARCHAR)")
    con.execute(
        "CREATE TABLE fct_shipments (shipment_id VARCHAR, customer_id VARCHAR, "
        "weight DECIMAL(10,2))"
    )
    # c2 orders but never ships; c3 ships but never orders.
    con.execute("INSERT INTO fct_orders VALUES ('1','c1',100,'paid'),('2','c2',50,'paid')")
    con.execute("INSERT INTO dim_customers VALUES ('c1','north'),('c2','south'),('c3','west')")
    con.execute("INSERT INTO fct_shipments VALUES ('s1','c1',7),('s3','c3',9)")

    rows = {r[0]: (r[1], r[2]) for r in con.execute(result.sql).fetchall()}
    assert set(rows) == {"north", "south", "west"}, "no group is dropped"
    assert rows["south"][1] is None, "no shipments is not a shipped weight of zero"
    assert rows["west"][0] is None, "no orders is not revenue of zero"


# ---------------------------------------------------------------------------
# S14 — reachability is enforced across all leaves
# ---------------------------------------------------------------------------


def test_s14_ac1_a_dimension_unreachable_from_one_leaf_names_that_leaf(
    resolver: ContractResolver, orders: SemanticSource, customers: SemanticSource
) -> None:
    """UNREACHABLE, naming both the dimension and the leaf that could not bind it (§5)."""
    isolated = SemanticSource(
        name="shipments",
        connection="w",
        table="fct_shipments",
        grain=["shipment_id"],
        columns=[
            Column(name="shipment_id", type="string", nullable=False),
            Column(name="weight", type="decimal", nullable=True),
        ],
        measures=[Measure(name="shipped_weight", expr="sum(weight)", additivity="additive")],
        dimensions=[],
    )
    with pytest.raises(exc.Unreachable) as raised:
        compile(
            SemanticQuery(metrics=["revenue", "shipped_weight"], dimensions=["region"]),
            resolver,
            [orders, customers, isolated],
        )
    assert "region" in str(raised.value)
    assert "shipments" in str(raised.value), "the message names the leaf that failed"


def test_s14_ac2_a_filter_resolving_on_only_some_leaves_is_unreachable(
    resolver: ContractResolver, orders: SemanticSource, customers: SemanticSource
) -> None:
    """A query filter is never applied to a subset of the metrics."""
    isolated = SemanticSource(
        name="shipments",
        connection="w",
        table="fct_shipments",
        grain=["shipment_id"],
        columns=[
            Column(name="shipment_id", type="string", nullable=False),
            Column(name="weight", type="decimal", nullable=True),
        ],
        measures=[Measure(name="shipped_weight", expr="sum(weight)", additivity="additive")],
        dimensions=[],
    )
    with pytest.raises(exc.Unreachable):
        compile(
            SemanticQuery(metrics=["revenue", "shipped_weight"], filters=["status = 'paid'"]),
            resolver,
            [orders, customers, isolated],
        )


# ---------------------------------------------------------------------------
# S15 — fail fast, never a partial result
# ---------------------------------------------------------------------------


def test_s15_ac1_one_unresolved_metric_fails_the_whole_query(
    resolver: ContractResolver, sources: list[SemanticSource]
) -> None:
    with pytest.raises(exc.Unresolved):
        compile(SemanticQuery(metrics=["revenue", "nonsense", "order_count"]), resolver, sources)


def test_s15_ac2_the_error_names_every_failing_metric(
    resolver: ContractResolver, sources: list[SemanticSource]
) -> None:
    """One round trip, not one recompile per bad name."""
    with pytest.raises(exc.Unresolved) as raised:
        compile(SemanticQuery(metrics=["revenue", "nonsense", "also_nonsense"]), resolver, sources)
    assert "nonsense" in str(raised.value)
    assert "also_nonsense" in str(raised.value)


def test_s15_ac2_unresolved_takes_precedence_over_ambiguous(
    sources: list[SemanticSource],
) -> None:
    """Both failure kinds at once report UNRESOLVED, and mention the ambiguous names too.

    An unresolved name is the worse failure: the caller has to find a different name, not
    merely disambiguate one that exists. Reporting the lower exit code first is the more
    actionable choice for a headless caller.
    """
    duplicated = [
        MetricBinding(
            metric="revenue",
            canonical=CanonicalRef(source="orders", measure="revenue"),
            provenance=Provenance.HUMAN_CURATED,
        ),
        MetricBinding(
            metric="revenue",
            canonical=CanonicalRef(source="orders", measure="order_count"),
            provenance=Provenance.HUMAN_CURATED,
        ),
    ]
    resolver = ContractResolver(bindings=duplicated, guardrails=[])
    with pytest.raises(exc.Unresolved) as raised:
        compile(SemanticQuery(metrics=["nonsense", "revenue"]), resolver, sources)
    assert raised.value.code is exc.ErrorCode.UNRESOLVED
    assert "nonsense" in str(raised.value)
    assert "also ambiguous" in str(raised.value)


# ---------------------------------------------------------------------------
# S16 — determinism
# ---------------------------------------------------------------------------


@pytest.mark.release_gate
def test_s16_ac1_multi_metric_compile_is_byte_identical_on_repeat(
    guarded_resolver: ContractResolver, sources: list[SemanticSource]
) -> None:
    """Same query, same SQL — including CTE names and their order (§6)."""
    query = SemanticQuery(
        metrics=["revenue", "shipped_weight", "avg_order_value"], dimensions=["region"]
    )
    first = compile(query, guarded_resolver, sources)
    second = compile(query, guarded_resolver, sources)
    assert first.sql == second.sql
    assert [g.id for g in first.guardrails_fired] == [g.id for g in second.guardrails_fired]


@pytest.mark.release_gate
def test_s16_ac2_metric_order_changes_columns_not_determinism(
    resolver: ContractResolver, sources: list[SemanticSource]
) -> None:
    """A different request order is a different input, so different SQL is correct.

    Each order is deterministic in itself, the CTEs are named by plan rather than by
    request position, and the output columns follow what the caller asked for.
    """
    forward = SemanticQuery(metrics=["revenue", "shipped_weight"], dimensions=["region"])
    reverse = SemanticQuery(metrics=["shipped_weight", "revenue"], dimensions=["region"])
    a, b = compile(forward, resolver, sources), compile(reverse, resolver, sources)

    assert a.sql == compile(forward, resolver, sources).sql
    assert b.sql == compile(reverse, resolver, sources).sql
    assert _cte_names(a.sql) == _cte_names(b.sql), "CTE names follow plan, not request order"
    assert [p.alias for p in sqlglot.parse_one(a.sql, dialect="postgres").expressions] == [
        "region",
        "revenue",
        "shipped_weight",
    ]
    assert [p.alias for p in sqlglot.parse_one(b.sql, dialect="postgres").expressions] == [
        "region",
        "shipped_weight",
        "revenue",
    ]


# ---------------------------------------------------------------------------
# S17 — metadata merges conservatively
# ---------------------------------------------------------------------------


def test_s17_ac1_one_stale_source_caps_the_whole_result(
    resolver: ContractResolver, sources: list[SemanticSource]
) -> None:
    """A result mixing a stale source and a fresh one is a stale result (§7).

    Asserted against ``freshness_signal``, which is where "stale" is actually realised:
    there is no result-level ``stale`` flag on ``QueryMetadata``, only a per-source one
    that P0 never sets to True. What multi-metric compose contributes is the *input* —
    one freshness entry per distinct source across every leaf — so a stale source
    reachable from any metric is in the list the signal reads.
    """
    from canonic.compiler.result import SourceFreshness
    from canonic.trust.signals import freshness_signal

    result = compile(
        SemanticQuery(metrics=["revenue", "shipped_weight"], dimensions=["region"]),
        resolver,
        sources,
    )
    assert {f.source for f in result.freshness} == {"orders", "customers", "shipments"}
    assert [f.source for f in result.freshness] == sorted(f.source for f in result.freshness)

    fresh = [
        SourceFreshness(source=f.source, last_validated_at=None, stale=False)
        for f in result.freshness
    ]
    one_stale = [
        *fresh[:-1],
        SourceFreshness(source="shipments", last_validated_at=None, stale=True),
    ]
    assert freshness_signal(one_stale).cap is not None, "any stale source caps the result"
    assert freshness_signal(fresh).cap is None


def test_s17_ac2_finality_merges_to_the_earliest_watermark_across_leaves() -> None:
    """One provisional leaf among several makes the merged finality provisional (§7).

    Earliest watermark, not latest: a result is only settled as far back as its
    least-settled input, and reporting the latest would claim settlement the data does
    not have. The emitted-SQL half of this — ``is_final`` joining through the grain spine
    so a provisional row can never pair with a final one — is pinned in
    ``test_composite_finality.py``.
    """
    from canonic.compiler.compose import _merge_finality, _Physical
    from canonic.compiler.result import FinalityMetadata

    def stub(watermark: str, sources: list[str]) -> _Physical:
        physical = _Physical(key_leaf=None)  # type: ignore[arg-type]  # only .finality is read
        physical.finality = FinalityMetadata(
            watermark=watermark, sources_used=sources, result_flag="per_row"
        )
        return physical

    merged = _merge_finality(
        [
            stub("2024-05-14T00:00:00+00:00", ["orders"]),
            stub("2024-05-10T00:00:00+00:00", ["orders_rt"]),
        ]
    )
    assert merged is not None
    assert merged.watermark == "2024-05-10T00:00:00+00:00"
    assert merged.sources_used == ["orders", "orders_rt"]
    assert _merge_finality([_Physical(key_leaf=None)]) is None  # type: ignore[arg-type]


def test_s17_ac3_trust_inputs_cover_every_requested_metric(
    resolver: ContractResolver, sources: list[SemanticSource]
) -> None:
    """The worst tier across metrics wins, which needs every metric to reach the scorer.

    The aggregation rule itself predates this work — ``TrustScorer`` has always been
    worst-signal-dominates. What multi-metric compose has to get right is that a query's
    trust inputs describe *all* of its metrics, so a weak one cannot be scored away by
    being left out. Provenance cannot currently separate the tiers (every static signal
    floors at ``provisional`` in v1, SPEC-E14 §7), so the rule is asserted directly.
    """
    from canonic.trust.models import SignalVerdict, TrustTier
    from canonic.trust.scorer import TrustScorer

    result = compile(
        SemanticQuery(metrics=["revenue", "shipped_weight", "avg_order_value"]),
        resolver,
        sources,
    )
    assert {t.metric for t in result.trust_inputs} == {
        "revenue",
        "shipped_weight",
        "avg_order_value",
    }

    mixed = [
        SignalVerdict(cap=TrustTier.TRUSTED, reason="strongest metric"),
        SignalVerdict(cap=TrustTier.CAUTION, reason="weakest metric"),
    ]
    assert TrustScorer.score(mixed).tier is TrustTier.CAUTION
