"""A conversion metric as discovery and resolution describe it, on the rental example."""

from __future__ import annotations

from pathlib import Path

import pytest

from canonic.core.service import CanonicService

_RENTAL = Path(__file__).parents[2] / "examples" / "rental"


@pytest.fixture(scope="module")
def service() -> CanonicService:
    return CanonicService.from_project(_RENTAL)


def test_describe_offers_the_dimensions_of_the_base_metric_only(service: CanonicService) -> None:
    detail = service.describe_metric("payment_rate_5d")
    names = {d.name for d in detail.dimensions}
    assert {"pickup_date", "membership_tier", "status"} <= names
    # method lives on payments, the conversion side, which is never grouped.
    assert "method" not in names
    assert detail.kind == "conversion"
    assert detail.conversion is not None
    assert detail.conversion.model_dump() == {
        "base_metric": "rental_count",
        "conversion_metric": "paid_rentals",
        "entity": {"base": "rental_id", "conversion": "rental_id"},
        "base_time": "pickup_date",
        "conversion_time": "payment_date",
        "window": "5 days",
        "calculation": "conversion_rate",
        "constant_properties": [],
        "attribution": "last_touch",
    }


def test_list_metrics_names_both_metrics_as_components(service: CanonicService) -> None:
    summary = next(m for m in service.list_metrics() if m.metric == "revenue_paid_within_5d")
    assert summary.kind == "conversion"
    assert summary.components == ["rental_count", "rental_revenue"]
