"""Tests for the BindingKind strategy registry (canonic/contracts/kinds.py)."""

from __future__ import annotations

import pytest

from canonic.contracts.kinds import (
    COMPOSITE_KINDS,
    DESCRIBABLE_KINDS,
    RECOMPUTE_KINDS,
    SOURCE_BOUND_KINDS,
    BindingKindSpec,
    register,
    spec_for,
)
from canonic.contracts.models import BindingKind, CanonicalRef


def test_every_binding_kind_has_a_spec() -> None:
    """The registry must cover every BindingKind member — consumers rely on spec_for never missing."""
    for kind in BindingKind:
        spec = spec_for(kind)
        assert spec.kind is kind


def test_spec_for_unregistered_kind_raises() -> None:
    """spec_for on an unregistered kind is a programming error, surfaced as ValueError."""
    with pytest.raises(ValueError, match="no BindingKindSpec registered"):
        spec_for("not_a_kind")  # type: ignore[arg-type]


def test_register_rejects_duplicate() -> None:
    """Re-registering a kind is refused (mirrors ConnectorFactory: no silent overwrite)."""
    dup = BindingKindSpec(
        BindingKind.SINGLE,
        is_source_bound=True,
        is_composite=False,
        is_recompute=False,
        is_describable=True,
        column_attr="measure",
        component_attrs=None,
    )
    with pytest.raises(ValueError, match="already registered"):
        register(dup)


def test_category_sets_match_expected_membership() -> None:
    """The derived category sets must match the kinds each consumer used to branch on."""
    assert {
        BindingKind.SINGLE,
        BindingKind.SEMI_ADDITIVE,
        BindingKind.DISTINCT_COUNT,
        BindingKind.PERCENTILE,
        BindingKind.OPAQUE,
    } == SOURCE_BOUND_KINDS
    assert {BindingKind.RATIO, BindingKind.WEIGHTED_AVG} == COMPOSITE_KINDS
    assert {BindingKind.DISTINCT_COUNT, BindingKind.PERCENTILE} == RECOMPUTE_KINDS
    assert {
        BindingKind.SINGLE,
        BindingKind.SEMI_ADDITIVE,
        BindingKind.DISTINCT_COUNT,
        BindingKind.PERCENTILE,
    } == DESCRIBABLE_KINDS


def test_source_bound_and_composite_are_disjoint_and_total() -> None:
    """Every kind is exactly one of source-bound or composite."""
    assert SOURCE_BOUND_KINDS.isdisjoint(COMPOSITE_KINDS)
    assert set(BindingKind) == SOURCE_BOUND_KINDS | COMPOSITE_KINDS


def test_column_field_reads_the_right_attribute() -> None:
    """column_field returns the physical column for source-bound kinds, None for composite."""
    single = CanonicalRef(kind=BindingKind.SINGLE, source="orders", measure="revenue")
    assert spec_for(single.kind).column_field(single) == "revenue"

    distinct = CanonicalRef(
        kind=BindingKind.DISTINCT_COUNT, source="orders", distinct_on="customer_id"
    )
    assert spec_for(distinct.kind).column_field(distinct) == "customer_id"

    pct = CanonicalRef(kind=BindingKind.PERCENTILE, source="orders", column="amount", quantile=0.5)
    assert spec_for(pct.kind).column_field(pct) == "amount"

    ratio = CanonicalRef(kind=BindingKind.RATIO, numerator="a", denominator="b")
    assert spec_for(ratio.kind).column_field(ratio) is None


def test_component_names_reads_the_right_attributes() -> None:
    """component_names returns the two sub-metric names for composite kinds, else (None, None)."""
    ratio = CanonicalRef(kind=BindingKind.RATIO, numerator="num", denominator="den")
    assert spec_for(ratio.kind).component_names(ratio) == ("num", "den")

    wavg = CanonicalRef(kind=BindingKind.WEIGHTED_AVG, weighted_sum="ws", weight="w")
    assert spec_for(wavg.kind).component_names(wavg) == ("ws", "w")

    single = CanonicalRef(kind=BindingKind.SINGLE, source="orders", measure="revenue")
    assert spec_for(single.kind).component_names(single) == (None, None)
