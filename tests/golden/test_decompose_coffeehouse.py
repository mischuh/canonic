"""``decompose`` on the shipped coffeehouse example, one case per method and one by week label.

The expected numbers are checked by hand against ``examples/coffeehouse/setup.sql``. January to
February 2026, completed orders of real customers only: revenue 295.00 to 370.00, cost of goods
sold -122.00 to -176.00, units 30 to 27. If one of these fails, first decide whether the number
is now right or wrong, as for the rest of the golden suite.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from canonic.core.decompose_models import (
    AdditiveBreakdown,
    DecomposeMethodName,
    DecomposeRequest,
    EffectKind,
    PartsBreakdown,
    ShiftShareBreakdown,
)
from canonic.exc import UnsupportedMeasure

if TYPE_CHECKING:
    from canonic.core.decompose_models import DecomposeResult
    from canonic.core.service import CanonicService

pytestmark = pytest.mark.release_gate


async def _decompose(
    services: dict[str, CanonicService],
    metric: str,
    by: list[str],
    period: dict[str, str] | None = None,
) -> DecomposeResult:
    request = DecomposeRequest.model_validate(
        {
            "metric": metric,
            "period": period
            or {"dimension": "order_month", "base": "2026-01", "compare": "2026-02"},
            "by": by,
        }
    )
    return await services["coffeehouse"].decompose(request)


async def test_revenue_by_category_is_additive(golden_services: dict[str, CanonicService]) -> None:
    result = await _decompose(golden_services, "revenue", ["category"])
    split = result.by["category"]

    assert result.method is DecomposeMethodName.ADDITIVE
    assert (result.total.base, result.total.compare) == (295.0, 370.0)
    assert isinstance(split, AdditiveBreakdown)
    assert {r.member: r.contribution for r in split.rows} == {
        "equipment": 60.0,
        "accessories": 15.0,
        "coffee": 0.0,
    }


async def test_gross_margin_rate_splits_by_numerator_parts(
    golden_services: dict[str, CanonicService],
) -> None:
    result = await _decompose(golden_services, "gross_margin_rate", ["category"])
    effects = {e.effect: e for e in result.effects}
    split = result.by["category"]

    assert result.method is DecomposeMethodName.RATIO_PARTS
    assert result.total.base == pytest.approx(173 / 295)
    assert result.total.compare == pytest.approx(194 / 370)
    assert effects[EffectKind.PART].metric == "cogs"
    assert effects[EffectKind.PART].contribution == pytest.approx(-54 * (1 / 295 + 1 / 370) / 2)
    assert effects[EffectKind.DENOMINATOR].contribution == pytest.approx(-149 * (1 / 370 - 1 / 295))
    assert isinstance(split, PartsBreakdown)
    cogs = {r.member: r.contribution for r in split.parts["cogs"].rows}
    assert cogs["equipment"] == pytest.approx(-39 * (1 / 295 + 1 / 370) / 2)
    assert sum(cogs.values()) == pytest.approx(effects[EffectKind.PART].contribution)
    assert [(r.member, r.delta) for r in split.denominator.rows] == [
        ("equipment", 60.0),
        ("accessories", 15.0),
        ("coffee", 0.0),
    ]


async def test_revenue_per_unit_rises_by_mix(golden_services: dict[str, CanonicService]) -> None:
    result = await _decompose(golden_services, "revenue_per_unit", ["category"])
    split = result.by["category"]

    assert result.method is DecomposeMethodName.RATIO_SHIFT_SHARE
    assert result.total.base == pytest.approx(295 / 30)
    assert result.total.compare == pytest.approx(370 / 27)
    assert isinstance(split, ShiftShareBreakdown)
    equipment = next(r for r in split.rows if r.member == "equipment")
    # A grinder costs 60 in both months, so its whole contribution is mix: 1 of 30 units
    # in January, 2 of 27 in February.
    assert equipment.rate == 0.0
    assert equipment.mix == pytest.approx((2 / 27 - 1 / 30) * 60)
    assert sum(r.contribution for r in split.rows) == pytest.approx(result.total.delta)


async def test_average_order_value_has_overall_effects_only(
    golden_services: dict[str, CanonicService],
) -> None:
    result = await _decompose(golden_services, "average_order_value", [])

    assert (result.total.base, result.total.compare) == (59.0, 74.0)
    assert sum(e.contribution for e in result.effects) == pytest.approx(15.0)
    with pytest.raises(UnsupportedMeasure, match="order_count' has kind distinct_count"):
        await _decompose(golden_services, "average_order_value", ["category"])


async def test_revenue_by_week_label(golden_services: dict[str, CanonicService]) -> None:
    # order_week is a label in the form 2026-05 with time_format %G-%V. ISO week 5 runs from
    # 26 January to 1 February: orders 6 and 7. Week 6 holds orders 8 to 10. Without the format,
    # 2026-05 would be read as May.
    week = {"dimension": "order_week", "base": "2026-05", "compare": "2026-06"}
    result = await _decompose(golden_services, "revenue", ["category"], week)
    split = result.by["category"]

    assert (result.total.base, result.total.compare) == (45.0, 345.0)
    assert isinstance(split, AdditiveBreakdown)
    assert {r.member: r.contribution for r in split.rows} == {
        "equipment": 120.0,
        "coffee": 105.0,
        "accessories": 75.0,
    }
    assert not any("not checked" in w for w in result.metadata.warnings)
