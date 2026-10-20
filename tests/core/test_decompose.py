"""The ``decompose`` capability end to end, against a small DuckDB project."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import pytest

from canonic.compiler import SemanticQuery
from canonic.core.decompose_models import (
    AdditiveBreakdown,
    DecomposeMethodName,
    DecomposeRequest,
    PartsBreakdown,
    Presence,
    ShiftShareBreakdown,
)
from canonic.core.service import CanonicService
from canonic.exc import (
    Ambiguous,
    CapabilityDisabled,
    DecompositionInconsistent,
    PeriodOpen,
    Unreachable,
    UnsupportedMeasure,
    ValidationFailed,
)

if TYPE_CHECKING:
    from pathlib import Path

# Two closed months of sales, a refunded order the guardrail strips, a month outside the
# compared periods, and a phone channel that only sells in September.
_SALES = [
    (1, "2025-07-15", "north", "web", 999.0, -1.0, 9, "completed"),
    (2, "2025-08-03", "north", "web", 400.0, -300.0, 4, "completed"),
    (3, "2025-08-10", "north", "store", 200.0, -200.0, 2, "completed"),
    (4, "2025-08-20", "south", "web", 400.0, -380.0, 5, "completed"),
    (5, "2025-09-02", "north", "web", 500.0, -400.0, 5, "completed"),
    (6, "2025-09-09", "north", "store", 200.0, -160.0, 2, "completed"),
    (7, "2025-09-15", "south", "store", 300.0, -250.0, 3, "completed"),
    (8, "2025-09-25", "south", "phone", 200.0, -180.0, 1, "completed"),
    (9, "2025-09-28", "north", "web", 1000.0, 0.0, 10, "refunded"),
]

_SALES_YAML = """\
name: sales
connection: warehouse_duckdb
table: sales
grain: [sale_id]
columns:
  - {name: sale_id, type: int, nullable: false}
  - {name: sale_date, type: date, nullable: false}
  - {name: region_code, type: string, nullable: false}
  - {name: channel, type: string, nullable: false}
  - {name: revenue, type: decimal, nullable: false}
  - {name: cost, type: decimal, nullable: false}
  - {name: units, type: int, nullable: false}
  - {name: status, type: string, nullable: false}
measures:
  - {name: revenue, expr: "sum(revenue)", additivity: additive}
  - {name: costs, expr: "sum(cost)", additivity: additive}
  - {name: net_result, expr: "sum(revenue + cost)", additivity: additive}
  - {name: units, expr: "sum(units)", additivity: additive}
dimensions:
  - {name: sale_date, column: sale_date, granularity: day}
  - {name: sale_month, column: sale_date, granularity: month}
  - {name: channel, column: channel, aliases: ["sales channel"]}
  - {name: status, column: status}
joins:
  - {to: regions, on: "sales.region_code = regions.region_code", relationship: many_to_one}
"""

_REGIONS_YAML = """\
name: regions
connection: warehouse_duckdb
table: regions
grain: [region_code]
columns:
  - {name: region_code, type: string, nullable: false}
  - {name: region_name, type: string, nullable: false}
dimensions:
  - {name: region, column: region_name}
"""

_TARGETS_YAML = """\
name: targets
connection: warehouse_duckdb
table: targets
grain: [target_month, team]
columns:
  - {name: target_month, type: string, nullable: false}
  - {name: team, type: string, nullable: false}
  - {name: amount, type: decimal, nullable: false}
measures:
  - {name: target, expr: "sum(amount)", additivity: additive}
dimensions:
  - {name: target_month, column: target_month, time_grain: month}
  - {name: team, column: team}
"""

# Weekly bookings with three spellings of one week: an ISO week without W and a declared
# time_format, the same label without one, and a sprint code no known form reads.
_WEEKLY_YAML = """\
name: weekly
connection: warehouse_duckdb
table: weekly
grain: [year_week, team]
columns:
  - {name: year_week, type: string, nullable: false}
  - {name: sprint, type: string, nullable: false}
  - {name: team, type: string, nullable: false}
  - {name: amount, type: decimal, nullable: false}
measures:
  - {name: booked, expr: "sum(amount)", additivity: additive}
dimensions:
  - {name: booking_week, column: year_week, time_grain: week, time_format: "%G-%V"}
  - {name: plain_week, column: year_week, time_grain: week}
  - {name: sprint, column: sprint, time_grain: week}
  - {name: crew, column: team}
"""

_METRICS = {
    "revenue": "canonical: {source: sales, measure: revenue}",
    "costs": "canonical: {source: sales, measure: costs}",
    "net_result": "canonical: {source: sales, measure: net_result}",
    "units": "canonical: {source: sales, measure: units}",
    "target": "canonical: {source: targets, measure: target}",
    "booked": "canonical: {source: weekly, measure: booked}",
    "revenue_echo": "canonical: {source: sales, measure: revenue}",
    "buyers": "canonical: {kind: distinct_count, source: sales, distinct_on: sale_id}",
    "price": "canonical: {kind: ratio, numerator: revenue, denominator: units}",
    "plain_rate": "canonical: {kind: ratio, numerator: net_result, denominator: revenue}",
    "echo_rate": "canonical: {kind: ratio, numerator: revenue_echo, denominator: revenue}",
    "web_revenue": (
        "canonical: {source: sales, measure: revenue, population_filter: \"channel = 'web'\"}"
    ),
    "web_north_revenue": (
        "canonical:\n  source: sales\n  measure: revenue\n"
        "  population_filter: \"(channel = 'web') AND region_code = 'north'\""
    ),
    "store_revenue": (
        "canonical: {source: sales, measure: revenue, population_filter: \"channel = 'store'\"}"
    ),
    "web_share": "canonical: {kind: ratio, numerator: web_revenue, denominator: revenue}",
    "web_north_share": (
        "canonical: {kind: ratio, numerator: web_north_revenue, denominator: web_revenue}"
    ),
    "store_per_web": "canonical: {kind: ratio, numerator: store_revenue, denominator: web_revenue}",
    "result_rate": (
        "canonical:\n  kind: ratio\n  numerator: net_result\n  denominator: revenue\n"
        "  numerator_parts: [revenue, costs]"
    ),
    "broken_rate": (
        "canonical:\n  kind: ratio\n  numerator: net_result\n  denominator: revenue\n"
        "  numerator_parts: [revenue, units]"
    ),
    "buyer_rate": "canonical: {kind: ratio, numerator: revenue, denominator: buyers}",
    "nested_rate": "canonical: {kind: ratio, numerator: price, denominator: units}",
}


@pytest.fixture
def decompose_project(tmp_path: Path) -> Path:
    import duckdb

    db_path = tmp_path / "warehouse.duckdb"
    con = duckdb.connect(str(db_path))
    con.execute(
        "CREATE TABLE sales (sale_id INTEGER, sale_date DATE, region_code VARCHAR, "
        "channel VARCHAR, revenue DECIMAL(12,2), cost DECIMAL(12,2), units INTEGER, "
        "status VARCHAR)"
    )
    con.executemany("INSERT INTO sales VALUES (?, ?, ?, ?, ?, ?, ?, ?)", _SALES)
    con.execute("CREATE TABLE regions (region_code VARCHAR, region_name VARCHAR)")
    con.execute("INSERT INTO regions VALUES ('north', 'North'), ('south', 'South')")
    con.execute("CREATE TABLE targets (target_month VARCHAR, team VARCHAR, amount DECIMAL(12,2))")
    con.execute(
        "INSERT INTO targets VALUES ('2025-08', 'a', 100), ('2025-08', 'b', 50), "
        "('2025-09', 'a', 120), ('2025-09', 'b', 40), ('2025-10', 'a', 999)"
    )
    con.execute(
        "CREATE TABLE weekly (year_week VARCHAR, sprint VARCHAR, team VARCHAR, "
        "amount DECIMAL(12,2))"
    )
    con.execute(
        "INSERT INTO weekly VALUES ('2025-42', 'S41', 'a', 10), ('2025-42', 'S41', 'b', 20), "
        "('2025-43', 'S42', 'a', 15), ('2025-43', 'S42', 'b', 30), ('2025-44', 'S43', 'a', 99)"
    )
    con.close()

    (tmp_path / "canonic.yaml").write_text(
        "version: 1\n"
        "project:\n  name: test\n  default_connection: warehouse_duckdb\n"
        "connections:\n"
        f"  - id: warehouse_duckdb\n    type: duckdb\n    params: {{path: {db_path}}}\n"
    )
    semantics = tmp_path / "semantics" / "warehouse_duckdb"
    semantics.mkdir(parents=True)
    (semantics / "sales.yaml").write_text(_SALES_YAML)
    (semantics / "regions.yaml").write_text(_REGIONS_YAML)
    (semantics / "targets.yaml").write_text(_TARGETS_YAML)
    (semantics / "weekly.yaml").write_text(_WEEKLY_YAML)
    metrics = tmp_path / "contracts" / "metrics"
    metrics.mkdir(parents=True)
    for name, canonical in _METRICS.items():
        (metrics / f"{name}.yaml").write_text(f"metric: {name}\n{canonical}\nstatus: active\n")
    guardrails = tmp_path / "contracts" / "guardrails"
    guardrails.mkdir(parents=True)
    (guardrails / "completed-only.yaml").write_text(
        "id: sales-completed-only\napplies_to:\n  source: sales\nkind: mandatory_filter\n"
        "filter: \"status = 'completed'\"\nseverity: error\nrationale: Refunds are not sales.\n"
    )
    return tmp_path


@pytest.fixture
def service(decompose_project: Path) -> CanonicService:
    return CanonicService.from_project(decompose_project)


def _request(metric: str, by: list[str] | None = None, **extra: Any) -> DecomposeRequest:
    period = extra.pop(
        "period", {"dimension": "sale_month", "base": "2025-08", "compare": "2025-09"}
    )
    return DecomposeRequest.model_validate(
        {"metric": metric, "period": period, "by": by or [], **extra}
    )


async def _month_value(service: CanonicService, metric: str, month: str) -> float:
    result = await service.query(
        SemanticQuery(metrics=[metric], dimensions=["sale_month"], filters=["status = 'completed'"])
    )
    for row in result.result.rows:
        if str(row[0]).startswith(month):
            return float(row[1])
    raise AssertionError(f"no row for {month}")


class TestAdditive:
    async def test_totals_match_query_and_members_sum_to_the_change(
        self, service: CanonicService
    ) -> None:
        result = await service.decompose(_request("revenue", ["channel", "region"]))

        assert result.method is DecomposeMethodName.ADDITIVE
        assert result.total.base == await _month_value(service, "revenue", "2025-08")
        assert result.total.compare == await _month_value(service, "revenue", "2025-09")
        assert result.total.base == 1000.0
        assert result.total.compare == 1200.0
        assert result.effects == []
        for dimension in ("channel", "region"):
            split = result.by[dimension]
            assert isinstance(split, AdditiveBreakdown)
            assert sum(r.contribution for r in split.rows) == pytest.approx(result.total.delta)
            assert split.residual == pytest.approx(0.0)
        assert {r.member: r.contribution for r in result.by["region"].rows} == {
            "North": 100.0,
            "South": 100.0,
        }

    async def test_top_n_merges_the_rest(self, service: CanonicService) -> None:
        result = await service.decompose(_request("revenue", ["channel"], top_n=1))
        rows = result.by["channel"].rows

        # web (-300) and store (+300) tie on magnitude, so the member order decides.
        assert [(r.member, r.rest) for r in rows] == [("store", False), (None, True)]
        assert sum(r.contribution for r in rows) == pytest.approx(result.total.delta)

    async def test_a_label_time_dimension_is_filtered_by_its_values(
        self, service: CanonicService
    ) -> None:
        result = await service.decompose(
            _request(
                "target",
                ["team"],
                period={"dimension": "target_month", "base": "2025-08", "compare": "2025-09"},
            )
        )
        assert (result.total.base, result.total.compare) == (150.0, 160.0)
        assert {r.member: r.contribution for r in result.by["team"].rows} == {"a": 20.0, "b": -10.0}


class TestWeekLabels:
    @staticmethod
    def _week(dimension: str, base: str, compare: str, **extra: Any) -> DecomposeRequest:
        return _request(
            "booked",
            ["crew"],
            period={"dimension": dimension, "base": base, "compare": compare},
            **extra,
        )

    @pytest.mark.parametrize("dimension", ["booking_week", "plain_week", "sprint"])
    async def test_rows_are_matched_to_the_periods_by_their_label(
        self, service: CanonicService, dimension: str
    ) -> None:
        base, compare = ("S41", "S42") if dimension == "sprint" else ("2025-42", "2025-43")
        result = await service.decompose(self._week(dimension, base, compare))

        assert (result.total.base, result.total.compare) == (30.0, 45.0)
        assert {r.member: r.contribution for r in result.by["crew"].rows} == {
            "a": 5.0,
            "b": 10.0,
        }

    @pytest.mark.parametrize("dimension", ["booking_week", "plain_week"])
    async def test_an_open_week_is_refused(self, service: CanonicService, dimension: str) -> None:
        # 2025-10-22 is the Wednesday of ISO week 43.
        as_of = datetime(2025, 10, 22, tzinfo=UTC)
        with pytest.raises(PeriodOpen, match="the week 2025-43 has not ended"):
            await service.decompose(self._week(dimension, "2025-42", "2025-43", as_of=as_of))

    async def test_allow_open_period_names_the_week_as_the_data_spells_it(
        self, service: CanonicService
    ) -> None:
        request = self._week(
            "booking_week",
            "2025-42",
            "2025-43",
            as_of=datetime(2025, 10, 22, tzinfo=UTC),
            allow_open_period=True,
        )
        result = await service.decompose(request)

        assert [w for w in result.metadata.warnings if "has not ended" in w] == [
            "booking_week: the week 2025-43 has not ended, so its figures are incomplete"
        ]

    async def test_a_label_that_does_not_fit_the_time_format_is_rejected(
        self, service: CanonicService
    ) -> None:
        with pytest.raises(ValidationFailed, match="does not fit the time_format '%G-%V'"):
            await service.decompose(self._week("booking_week", "2025-W42", "2025-W43"))

    async def test_an_unreadable_label_is_decomposed_with_a_warning(
        self, service: CanonicService
    ) -> None:
        # S42 is no form canonic reads, so even a clock inside the sprint cannot refuse it.
        as_of = datetime(2025, 10, 22, tzinfo=UTC)
        result = await service.decompose(self._week("sprint", "S41", "S42", as_of=as_of))

        assert result.total.delta == 15.0
        assert (
            "sprint: 'S41' is not a week label canonic can read, so whether its period has "
            "ended was not checked; declare time_format on the dimension"
        ) in result.metadata.warnings

    async def test_the_same_label_twice_is_rejected(self, service: CanonicService) -> None:
        with pytest.raises(ValidationFailed, match="same week"):
            await service.decompose(self._week("sprint", "S41", " S41"))


class TestRatio:
    async def test_shift_share_effects_and_members_sum_to_the_change(
        self, service: CanonicService
    ) -> None:
        result = await service.decompose(_request("price", ["channel"]))
        split = result.by["channel"]

        assert result.method is DecomposeMethodName.RATIO_SHIFT_SHARE
        assert result.total.base == pytest.approx(1000 / 11)
        assert result.total.compare == pytest.approx(1200 / 11)
        assert sum(e.contribution for e in result.effects) == pytest.approx(result.total.delta)
        assert isinstance(split, ShiftShareBreakdown)
        assert sum(r.mix + r.rate for r in split.rows) == pytest.approx(result.total.delta)
        phone = next(r for r in split.rows if r.member == "phone")
        assert phone.presence is Presence.COMPARE_ONLY
        assert phone.rate == 0.0

    async def test_total_matches_a_query_of_the_ratio(self, service: CanonicService) -> None:
        result = await service.decompose(_request("price"))
        assert result.total.compare == pytest.approx(
            await _month_value(service, "price", "2025-09")
        )

    async def test_swapping_the_periods_negates_every_effect(self, service: CanonicService) -> None:
        forward = await service.decompose(_request("price", ["channel"]))
        backward = await service.decompose(
            _request(
                "price",
                ["channel"],
                period={"dimension": "sale_month", "base": "2025-09", "compare": "2025-08"},
            )
        )
        assert [e.contribution for e in backward.effects] == pytest.approx(
            [-e.contribution for e in forward.effects]
        )
        back_rows = {r.member: r for r in backward.by["channel"].rows}
        for row in forward.by["channel"].rows:
            assert back_rows[row.member].mix == pytest.approx(-row.mix)
            assert back_rows[row.member].rate == pytest.approx(-row.rate)

    async def test_numerator_parts(self, service: CanonicService) -> None:
        result = await service.decompose(_request("result_rate", ["region"]))
        split = result.by["region"]

        assert result.method is DecomposeMethodName.RATIO_PARTS
        assert result.total.base == pytest.approx(0.12)
        assert result.total.compare == pytest.approx(0.175)
        assert [e.metric for e in result.effects] == ["costs", "revenue"]
        assert sum(e.contribution for e in result.effects) == pytest.approx(result.total.delta)
        assert isinstance(split, PartsBreakdown)
        assert {r.member: r.contribution for r in split.parts["costs"].rows} == {
            "North": pytest.approx(-0.0550),
            "South": pytest.approx(-0.04583, abs=1e-5),
        }
        assert result.metadata.composition["result_rate"].numerator_parts == ["revenue", "costs"]

    async def test_parts_that_do_not_sum_fail(self, service: CanonicService) -> None:
        with pytest.raises(DecompositionInconsistent, match="broken_rate"):
            await service.decompose(_request("broken_rate"))

    async def test_shared_source_and_measure_warns(self, service: CanonicService) -> None:
        result = await service.decompose(_request("echo_rate"))
        assert result.method is DecomposeMethodName.RATIO_SHIFT_SHARE
        assert (
            "components of echo_rate share source and measure; if the numerator contains the "
            "denominator, declare numerator_parts"
        ) in result.metadata.warnings

    @pytest.mark.parametrize(
        ("metric", "warns"),
        [("web_share", False), ("web_north_share", False), ("store_per_web", True)],
    )
    async def test_a_narrower_numerator_population_does_not_warn(
        self, service: CanonicService, metric: str, warns: bool
    ) -> None:
        result = await service.decompose(_request(metric, ["region"]))
        assert result.method is DecomposeMethodName.RATIO_SHIFT_SHARE
        assert any("share source and measure" in w for w in result.metadata.warnings) is warns


class TestUnsupported:
    @pytest.mark.parametrize(
        ("metric", "by", "match"),
        [
            ("buyers", [], "distinct_count cannot be attributed by member: members overlap"),
            ("buyer_rate", ["channel"], "denominator 'buyers' has kind distinct_count"),
            ("nested_rate", [], "numerator 'price' has kind ratio"),
        ],
    )
    async def test_raises_unsupported_measure(
        self, service: CanonicService, metric: str, by: list[str], match: str
    ) -> None:
        with pytest.raises(UnsupportedMeasure, match=match):
            await service.decompose(_request(metric, by))

    async def test_a_distinct_count_component_has_overall_effects(
        self, service: CanonicService
    ) -> None:
        result = await service.decompose(_request("buyer_rate"))

        assert result.method is DecomposeMethodName.RATIO_SHIFT_SHARE
        assert result.total.base == pytest.approx(1000 / 3)
        assert result.total.compare == pytest.approx(1200 / 4)
        assert sum(e.contribution for e in result.effects) == pytest.approx(result.total.delta)
        assert result.by == {}


class TestDimensionNames:
    """A requested dimension may be an alias or qualified, the result column is named by it."""

    async def test_a_qualified_split_dimension(self, service: CanonicService) -> None:
        plain = await service.decompose(_request("result_rate", ["region"]))
        qualified = await service.decompose(_request("result_rate", ["regions.region"]))

        assert list(qualified.by) == ["regions.region"]
        assert qualified.by["regions.region"] == plain.by["region"]

    async def test_an_alias_as_split_dimension(self, service: CanonicService) -> None:
        plain = await service.decompose(_request("revenue", ["channel"]))
        aliased = await service.decompose(_request("revenue", ["sales channel"]))

        assert aliased.by["sales channel"] == plain.by["channel"]

    async def test_a_qualified_period_dimension(self, service: CanonicService) -> None:
        period = {"dimension": "sales.sale_month", "base": "2025-08", "compare": "2025-09"}
        result = await service.decompose(_request("revenue", ["channel"], period=period))

        assert (result.total.base, result.total.compare) == (1000.0, 1200.0)

    async def test_an_ambiguous_dimension_names_the_qualified_candidates(
        self, decompose_project: Path
    ) -> None:
        semantics = decompose_project / "semantics" / "warehouse_duckdb"
        sales = semantics / "sales.yaml"
        sales.write_text(
            sales.read_text()
            + '  - {to: districts, on: "sales.region_code = districts.code", '
            + "relationship: many_to_one}\n"
        )
        (semantics / "districts.yaml").write_text(
            "name: districts\nconnection: warehouse_duckdb\ntable: districts\ngrain: [code]\n"
            "columns:\n  - {name: code, type: string, nullable: false}\n"
            "  - {name: name, type: string, nullable: false}\n"
            "dimensions:\n  - {name: region, column: name}\n"
        )
        service = CanonicService.from_project(decompose_project)

        with pytest.raises(Ambiguous) as excinfo:
            await service.decompose(_request("revenue", ["region"]))
        assert "regions.region" in excinfo.value.candidates
        result = await service.decompose(_request("revenue", ["regions.region"]))
        assert {r.member for r in result.by["regions.region"].rows} == {"North", "South"}


class TestValidation:
    @pytest.mark.parametrize(
        ("extra", "match"),
        [
            ({"period": {"dimension": "channel", "base": "web", "compare": "store"}}, "no period"),
            (
                {"period": {"dimension": "sale_month", "base": "2025-08", "compare": "2025-08"}},
                "same month",
            ),
            ({"filters": ["sale_month >= '2025-08-01'"]}, "restricts the period dimension"),
            ({"by": ["channel", "region", "status", "sale_date"]}, "at most 3"),
            ({"by": ["channel", "channel"]}, "more than once"),
            (
                {"period": {"dimension": "sale_month", "base": "August", "compare": "2025-09"}},
                "not a month",
            ),
        ],
    )
    async def test_rejects_the_request(
        self, service: CanonicService, extra: dict[str, Any], match: str
    ) -> None:
        by = extra.pop("by", None)
        with pytest.raises(ValidationFailed, match=match):
            await service.decompose(_request("revenue", by, **extra))

    async def test_unreachable_dimension_names_the_leaf(self, service: CanonicService) -> None:
        with pytest.raises(Unreachable, match="'team' is not reachable .* leaf metric 'revenue'"):
            await service.decompose(_request("revenue", ["team"]))

    async def test_disabled_capability(self, decompose_project: Path) -> None:
        config = decompose_project / "canonic.yaml"
        config.write_text(config.read_text() + "decompose:\n  enabled: false\n")
        service = CanonicService.from_project(decompose_project)
        with pytest.raises(CapabilityDisabled, match="decompose.enabled"):
            await service.decompose(_request("revenue"))


class TestOpenPeriod:
    _AS_OF = datetime(2025, 9, 20, tzinfo=UTC)

    async def test_an_open_period_is_refused(self, service: CanonicService) -> None:
        with pytest.raises(PeriodOpen, match="month 2025-09 has not ended"):
            await service.decompose(_request("revenue", as_of=self._AS_OF))

    async def test_allow_open_period_warns_and_caps_the_tier(self, service: CanonicService) -> None:
        result = await service.decompose(
            _request("revenue", ["channel"], as_of=self._AS_OF, allow_open_period=True)
        )
        assert [w for w in result.metadata.warnings if "has not ended" in w] == [
            "sale_month: the month 2025-09 has not ended, so its figures are incomplete"
        ]
        assert result.metadata.trust_score is not None
        assert result.metadata.trust_score.tier in {"provisional", "caution"}


class TestServing:
    async def test_runs_one_statement_per_split_plus_one(
        self, service: CanonicService, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ctx = service._ctx
        executed: list[str] = []
        original = ctx.execute_compiled

        async def counting(compiled: Any, connection_id: str | None) -> Any:
            executed.append(compiled.sql)
            return await original(compiled, connection_id)

        monkeypatch.setattr(ctx, "execute_compiled", counting)
        await service.decompose(_request("result_rate", ["channel", "region"]))
        assert len(executed) == 3

    async def test_guardrails_match_a_query_of_the_leaves(self, service: CanonicService) -> None:
        result = await service.decompose(_request("result_rate", ["channel"]))
        leaves = await service.query(
            SemanticQuery(metrics=["net_result", "revenue", "costs"], dimensions=["channel"])
        )
        assert result.metadata.guardrails_fired == leaves.metadata.guardrails_fired
        assert result.metadata.scope == leaves.metadata.scope

    async def test_identical_calls_return_identical_results(self, service: CanonicService) -> None:
        first = await service.decompose(_request("price", ["channel", "region"]))
        second = await service.decompose(_request("price", ["channel", "region"]))
        assert json.dumps(first.model_dump(mode="json")) == json.dumps(
            second.model_dump(mode="json")
        )

    async def test_writes_one_answer_event(
        self, service: CanonicService, decompose_project: Path
    ) -> None:
        await service.decompose(_request("result_rate", ["channel"]))
        lines = (decompose_project / ".canonic" / "events.jsonl").read_text().splitlines()
        events = [json.loads(line) for line in lines]

        assert len(events) == 1
        assert events[0]["capability"] == "decompose"
        assert events[0]["method"] == "ratio_parts"
        assert events[0]["guardrails_fired"] == ["sales-completed-only"]
