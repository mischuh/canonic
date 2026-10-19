"""The arithmetic of ``decompose``: exactness, symmetry and the worked example of each method."""

from __future__ import annotations

import pytest

from canonic.core.decompose_methods import (
    AdditiveMethod,
    LeafValues,
    MemberValues,
    PeriodLabels,
    RatioPartsMethod,
    RatioShiftShareMethod,
)
from canonic.core.decompose_models import (
    AdditiveBreakdown,
    EffectKind,
    PartsBreakdown,
    Presence,
    ShiftShareBreakdown,
)
from canonic.exc import DecompositionInconsistent, ValidationFailed

_LABELS = PeriodLabels("order_month", "2026-08", "2026-09")

# The worked example: result_rate = (revenue + costs) / revenue by region.
_OVERALL: LeafValues = {
    "net_result": (120.0, 210.0),
    "revenue": (1000.0, 1200.0),
    "costs": (-880.0, -990.0),
}
_REGIONS: MemberValues = {
    "north": {"net_result": (100.0, 140.0), "revenue": (600.0, 700.0), "costs": (-500.0, -560.0)},
    "south": {"net_result": (20.0, 70.0), "revenue": (400.0, 500.0), "costs": (-380.0, -430.0)},
}


def _swap(values: LeafValues) -> LeafValues:
    return {leaf: (pair[1], pair[0]) for leaf, pair in values.items()}


def _swap_members(members: MemberValues) -> MemberValues:
    return {member: _swap(values) for member, values in members.items()}


def _parts() -> RatioPartsMethod:
    return RatioPartsMethod(
        "result_rate", "net_result", "revenue", ["revenue", "costs"], _LABELS, 1e-9
    )


def _shift_share() -> RatioShiftShareMethod:
    return RatioShiftShareMethod("result_rate", "net_result", "revenue", _LABELS)


class TestAdditive:
    _members: MemberValues = {
        "a": {"revenue": (10.0, 30.0)},
        "b": {"revenue": (50.0, 40.0)},
        "c": {"revenue": (0.0, 5.0)},
    }
    _overall: LeafValues = {"revenue": (60.0, 75.0)}

    def test_rows_sum_to_the_total_change(self) -> None:
        method = AdditiveMethod("revenue")
        split = method.split(self._overall, self._members, 10)

        assert isinstance(split, AdditiveBreakdown)
        assert method.total(self._overall).delta == 15.0
        assert sum(r.contribution for r in split.rows) == pytest.approx(15.0)
        assert split.residual == pytest.approx(0.0)
        assert method.effects(self._overall) == []

    def test_rows_rank_by_absolute_contribution(self) -> None:
        split = AdditiveMethod("revenue").split(self._overall, self._members, 10)
        assert [r.member for r in split.rows] == ["a", "b", "c"]

    def test_rest_row_merges_members_beyond_top_n(self) -> None:
        members: MemberValues = {
            name: {"revenue": (0.0, float(value))}
            for name, value in [("a", 5), ("b", 4), ("c", 3), ("d", 2), ("e", 1)]
        }
        split = AdditiveMethod("revenue").split({"revenue": (0.0, 15.0)}, members, 2)

        assert [(r.member, r.rest) for r in split.rows] == [
            ("a", False),
            ("b", False),
            (None, True),
        ]
        assert split.rows[-1].contribution == 6.0
        assert sum(r.contribution for r in split.rows) == 15.0

    def test_ties_order_by_member_with_null_last(self) -> None:
        members: MemberValues = {
            None: {"revenue": (0.0, 1.0)},
            "b": {"revenue": (0.0, 1.0)},
            "a": {"revenue": (0.0, -1.0)},
        }
        split = AdditiveMethod("revenue").split({"revenue": (0.0, 1.0)}, members, 10)
        assert [(r.member, r.rest) for r in split.rows] == [
            ("a", False),
            ("b", False),
            (None, False),
        ]


class TestRatioParts:
    def test_worked_example(self) -> None:
        method = _parts()
        method.check(_OVERALL)
        total = method.total(_OVERALL)
        effects = {e.effect: e for e in method.effects(_OVERALL)}
        split = method.split(_OVERALL, _REGIONS, 10)

        assert total.base == pytest.approx(0.12)
        assert total.compare == pytest.approx(0.175)
        assert effects[EffectKind.PART].metric == "costs"
        assert effects[EffectKind.PART].contribution == pytest.approx(-0.10083, abs=1e-5)
        assert effects[EffectKind.DENOMINATOR].contribution == pytest.approx(0.15583, abs=1e-5)
        assert isinstance(split, PartsBreakdown)
        costs = {r.member: r.contribution for r in split.parts["costs"].rows}
        assert costs == {
            "north": pytest.approx(-0.0550),
            "south": pytest.approx(-0.04583, abs=1e-5),
        }
        assert split.denominator.units == "denominator"
        assert [(r.member, r.delta) for r in split.denominator.rows] == [
            ("north", 100.0),
            ("south", 100.0),
        ]

    def test_a_member_without_a_denominator_has_no_denominator_row(self) -> None:
        # A member that only moves a part, like an expense category without income.
        members = {
            **_REGIONS,
            "fees": {"net_result": (-5.0, -8.0), "revenue": (0.0, 0.0), "costs": (-5.0, -8.0)},
        }
        split = _parts().split(_OVERALL, members, 10)

        assert isinstance(split, PartsBreakdown)
        assert "fees" in {r.member for r in split.parts["costs"].rows}
        assert [r.member for r in split.denominator.rows] == ["north", "south"]

    def test_effects_sum_to_the_total_change(self) -> None:
        method = _parts()
        assert sum(e.contribution for e in method.effects(_OVERALL)) == pytest.approx(
            method.total(_OVERALL).delta
        )

    def test_member_contributions_sum_to_their_part_effect(self) -> None:
        method = _parts()
        part_effect = next(e for e in method.effects(_OVERALL) if e.metric == "costs")
        rows = method.split(_OVERALL, _REGIONS, 10).parts["costs"].rows
        assert sum(r.contribution for r in rows) == pytest.approx(part_effect.contribution)

    def test_a_part_that_is_the_denominator_has_no_effect(self) -> None:
        effects = _parts().effects(_OVERALL)
        assert [e.metric for e in effects if e.effect is EffectKind.PART] == ["costs"]
        assert "revenue" not in _parts().split(_OVERALL, _REGIONS, 10).parts

    def test_parts_that_do_not_sum_to_the_numerator_fail(self) -> None:
        broken = {**_OVERALL, "net_result": (120.0, 211.0)}
        with pytest.raises(DecompositionInconsistent, match="order_month 2026-09"):
            _parts().check(broken)

    def test_swap_negates_every_contribution(self) -> None:
        method = _parts()
        forward = method.effects(_OVERALL)
        backward = method.effects(_swap(_OVERALL))
        for f, b in zip(forward, backward, strict=True):
            assert b.contribution == pytest.approx(-f.contribution)
        forward_rows = method.split(_OVERALL, _REGIONS, 10).parts["costs"].rows
        backward_rows = (
            method.split(_swap(_OVERALL), _swap_members(_REGIONS), 10).parts["costs"].rows
        )
        assert [r.contribution for r in backward_rows] == pytest.approx(
            [-r.contribution for r in forward_rows]
        )


class TestRatioShiftShare:
    def test_effects_match_the_worked_example(self) -> None:
        effects = {e.effect: e.contribution for e in _shift_share().effects(_OVERALL)}
        assert effects[EffectKind.NUMERATOR] == pytest.approx(0.0825)
        assert effects[EffectKind.DENOMINATOR] == pytest.approx(-0.0275)

    def test_effects_and_members_sum_to_the_total_change(self) -> None:
        method = _shift_share()
        delta = method.total(_OVERALL).delta
        split = method.split(_OVERALL, _REGIONS, 10)

        assert isinstance(split, ShiftShareBreakdown)
        assert sum(e.contribution for e in method.effects(_OVERALL)) == pytest.approx(delta)
        assert sum(r.mix + r.rate for r in split.rows) == pytest.approx(delta)
        assert split.residual == pytest.approx(0.0)

    def test_swap_negates_mix_and_rate(self) -> None:
        method = _shift_share()
        forward = {r.member: r for r in method.split(_OVERALL, _REGIONS, 10).rows}
        backward = {
            r.member: r for r in method.split(_swap(_OVERALL), _swap_members(_REGIONS), 10).rows
        }
        for member, row in forward.items():
            assert backward[member].mix == pytest.approx(-row.mix)
            assert backward[member].rate == pytest.approx(-row.rate)

    def test_a_member_only_in_the_compare_period_is_all_mix(self) -> None:
        members: MemberValues = {
            "north": {"net_result": (120.0, 150.0), "revenue": (1000.0, 1000.0)},
            "west": {"net_result": (0.0, 60.0), "revenue": (0.0, 200.0)},
        }
        overall: LeafValues = {"net_result": (120.0, 210.0), "revenue": (1000.0, 1200.0)}
        method = _shift_share()
        split = method.split(overall, members, 10)
        west = next(r for r in split.rows if r.member == "west")

        assert west.presence is Presence.COMPARE_ONLY
        assert west.rate == 0.0
        assert west.rate_base is None
        assert west.mix == pytest.approx(200 / 1200 * 60 / 200)
        assert sum(r.contribution for r in split.rows) == pytest.approx(method.total(overall).delta)

    def test_a_zero_denominator_cannot_be_decomposed(self) -> None:
        with pytest.raises(ValidationFailed, match="zero in order_month 2026-08"):
            _shift_share().check({"net_result": (1.0, 2.0), "revenue": (0.0, 5.0)})
