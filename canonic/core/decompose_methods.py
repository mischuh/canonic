"""The arithmetic of ``decompose``: one class per attribution method.

Every method works on aggregates already fetched from the warehouse: the value of each leaf
metric in the base and the compare period, overall and per member of a split dimension. Every
split is exact, the contributions sum to the total change up to floating point rounding, and it
is symmetric: swapping the two periods negates every contribution. The midpoint form
(``x̄ = (x0 + x1) / 2``) is what makes both hold.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from canonic.core.decompose_models import (
    AdditiveBreakdown,
    AdditiveRow,
    DecomposeMethodName,
    DenominatorBreakdown,
    DenominatorRow,
    Effect,
    EffectKind,
    PartBreakdown,
    PartsBreakdown,
    Presence,
    ShiftShareBreakdown,
    ShiftShareRow,
    Total,
)
from canonic.exc import DecompositionInconsistent, ValidationFailed

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence
    from typing import Any

__all__ = [
    "AdditiveMethod",
    "DecompositionMethod",
    "LeafValues",
    "MemberValues",
    "PeriodLabels",
    "RatioPartsMethod",
    "RatioShiftShareMethod",
]

#: The value of each leaf metric in the base and the compare period.
type LeafValues = Mapping[str, tuple[float, float]]
#: :data:`LeafValues` per member of one split dimension.
type MemberValues = Mapping[Any, LeafValues]


@dataclass(frozen=True, slots=True)
class PeriodLabels:
    """How the two periods are named in an error message."""

    dimension: str
    base: str
    compare: str


class DecompositionMethod(Protocol):
    """Attributes the change of one metric, given the values of the leaf metrics it needs."""

    @property
    def name(self) -> DecomposeMethodName: ...

    def leaf_metrics(self) -> list[str]:
        """The metrics to fetch, each of kind ``single``, in a stable order."""
        ...

    def check(self, overall: LeafValues) -> None:
        """Raise when the fetched values cannot be decomposed."""
        ...

    def total(self, overall: LeafValues) -> Total: ...

    def effects(self, overall: LeafValues) -> list[Effect]: ...

    def split(
        self, overall: LeafValues, members: MemberValues, top_n: int
    ) -> AdditiveBreakdown | ShiftShareBreakdown | PartsBreakdown: ...


def _value(values: LeafValues, metric: str) -> tuple[float, float]:
    return values.get(metric, (0.0, 0.0))


def _mid(pair: tuple[float, float]) -> float:
    return (pair[0] + pair[1]) / 2


def _delta(pair: tuple[float, float]) -> float:
    return pair[1] - pair[0]


def _member_key(member: Any) -> tuple[bool, int, float, str]:
    """Ascending member order: numbers by value, everything else by text, ``NULL`` last."""
    if member is None:
        return (True, 0, 0.0, "")
    if isinstance(member, int | float) and not isinstance(member, bool):
        return (False, 0, float(member), "")
    return (False, 1, 0.0, str(member))


def _ranked[R](
    rows: Sequence[R], magnitude: Callable[[R], float], member: Callable[[R], Any]
) -> list[R]:
    """Rows by absolute magnitude, descending, then by member ascending with ``NULL`` last."""
    return sorted(rows, key=lambda row: (-abs(magnitude(row)), _member_key(member(row))))


def _top[R](rows: list[R], top_n: int, merge: Callable[[list[R]], R]) -> list[R]:
    """The first ``top_n`` rows, plus one merged rest row when more remain."""
    if len(rows) <= top_n:
        return rows
    return [*rows[:top_n], merge(rows[top_n:])]


class _RatioTotals:
    """The overall values of a ratio's numerator and denominator in both periods."""

    def __init__(self, numerator: str, denominator: str, metric: str, labels: PeriodLabels) -> None:
        self.numerator = numerator
        self.denominator = denominator
        self._metric = metric
        self._labels = labels

    def check_denominator(self, overall: LeafValues) -> None:
        base, compare = _value(overall, self.denominator)
        for value, period in ((base, self._labels.base), (compare, self._labels.compare)):
            if value == 0:
                raise ValidationFailed(
                    f"metric {self._metric!r}: the denominator {self.denominator!r} is zero in "
                    f"{self._labels.dimension} {period}, so the ratio has no value to decompose"
                )

    def total(self, overall: LeafValues) -> Total:
        n0, n1 = _value(overall, self.numerator)
        d0, d1 = _value(overall, self.denominator)
        base, compare = n0 / d0, n1 / d1
        return Total(base=base, compare=compare, delta=compare - base)

    def inverse(self, overall: LeafValues) -> tuple[float, float]:
        """``avg(1/D)`` and ``Δ(1/D)``."""
        d0, d1 = _value(overall, self.denominator)
        return (1 / d0 + 1 / d1) / 2, 1 / d1 - 1 / d0


class AdditiveMethod:
    """``single``: the change is the sum of the members' changes."""

    name = DecomposeMethodName.ADDITIVE

    def __init__(self, metric: str) -> None:
        self._metric = metric

    def leaf_metrics(self) -> list[str]:
        return [self._metric]

    def check(self, overall: LeafValues) -> None:
        return None

    def total(self, overall: LeafValues) -> Total:
        base, compare = _value(overall, self._metric)
        return Total(base=base, compare=compare, delta=compare - base)

    def effects(self, overall: LeafValues) -> list[Effect]:
        return []

    def split(self, overall: LeafValues, members: MemberValues, top_n: int) -> AdditiveBreakdown:
        rows = _additive_rows(members, self._metric, 1.0, top_n)
        return AdditiveBreakdown(rows=rows, residual=self.total(overall).delta - _sum(rows))


class RatioShiftShareMethod:
    """``ratio``/``weighted_avg`` without numerator parts: numerator and denominator effect,
    and per member a mix effect (its share of the denominator moved) and a rate effect (its own
    ratio moved).
    """

    name = DecomposeMethodName.RATIO_SHIFT_SHARE

    def __init__(self, metric: str, numerator: str, denominator: str, labels: PeriodLabels) -> None:
        self._ratio = _RatioTotals(numerator, denominator, metric, labels)

    def leaf_metrics(self) -> list[str]:
        return list(dict.fromkeys([self._ratio.numerator, self._ratio.denominator]))

    def check(self, overall: LeafValues) -> None:
        self._ratio.check_denominator(overall)

    def total(self, overall: LeafValues) -> Total:
        return self._ratio.total(overall)

    def effects(self, overall: LeafValues) -> list[Effect]:
        numerator = _value(overall, self._ratio.numerator)
        denominator = _value(overall, self._ratio.denominator)
        avg_inverse, delta_inverse = self._ratio.inverse(overall)
        return [
            Effect(
                effect=EffectKind.NUMERATOR,
                metric=self._ratio.numerator,
                base=numerator[0],
                compare=numerator[1],
                contribution=_delta(numerator) * avg_inverse,
            ),
            Effect(
                effect=EffectKind.DENOMINATOR,
                metric=self._ratio.denominator,
                base=denominator[0],
                compare=denominator[1],
                contribution=_mid(numerator) * delta_inverse,
            ),
        ]

    def split(self, overall: LeafValues, members: MemberValues, top_n: int) -> ShiftShareBreakdown:
        total_d0, total_d1 = _value(overall, self._ratio.denominator)
        rows: list[ShiftShareRow] = []
        for member, values in members.items():
            row = self._row(member, values, total_d0, total_d1)
            if row is not None:
                rows.append(row)
        ranked = _ranked(rows, lambda r: r.contribution, lambda r: r.member)
        listed = _top(ranked, top_n, _merge_shift_share)
        return ShiftShareBreakdown(
            rows=listed,
            residual=self.total(overall).delta - sum(r.contribution for r in listed),
        )

    def _row(
        self, member: Any, values: LeafValues, total_d0: float, total_d1: float
    ) -> ShiftShareRow | None:
        n0, n1 = _value(values, self._ratio.numerator)
        d0, d1 = _value(values, self._ratio.denominator)
        s0, s1 = d0 / total_d0, d1 / total_d1
        if d0 != 0 and d1 != 0:
            r0, r1 = n0 / d0, n1 / d1
            mix = (s1 - s0) * (r0 + r1) / 2
            rate = (s0 + s1) / 2 * (r1 - r0)
            return ShiftShareRow(
                member=member,
                share_base=s0,
                share_compare=s1,
                rate_base=r0,
                rate_compare=r1,
                mix=mix,
                rate=rate,
                contribution=mix + rate,
                presence=Presence.BOTH,
            )
        if d1 != 0:
            r1 = n1 / d1
            return ShiftShareRow(
                member=member,
                share_base=0.0,
                share_compare=s1,
                rate_base=None,
                rate_compare=r1,
                mix=s1 * r1,
                rate=0.0,
                contribution=s1 * r1,
                presence=Presence.COMPARE_ONLY,
            )
        if d0 != 0:
            r0 = n0 / d0
            return ShiftShareRow(
                member=member,
                share_base=s0,
                share_compare=0.0,
                rate_base=r0,
                rate_compare=None,
                mix=-s0 * r0,
                rate=0.0,
                contribution=-s0 * r0,
                presence=Presence.BASE_ONLY,
            )
        # No denominator in either period: the member has no share and no rate to attribute.
        return None


class RatioPartsMethod:
    """``ratio``/``weighted_avg`` with ``numerator_parts``: one effect per part plus one
    denominator effect. A part that is the denominator is the constant ``1`` and drops out.

    Each part is additive, so its effect splits exactly by member. The denominator effect does
    not, because ``1/D`` is not linear in the members. The members' denominator moves are listed
    in the denominator's own units instead.
    """

    name = DecomposeMethodName.RATIO_PARTS

    def __init__(
        self,
        metric: str,
        numerator: str,
        denominator: str,
        parts: Sequence[str],
        labels: PeriodLabels,
        tolerance: float,
    ) -> None:
        self._metric = metric
        self._ratio = _RatioTotals(numerator, denominator, metric, labels)
        self._parts = list(parts)
        self._labels = labels
        self._tolerance = tolerance

    @property
    def _moving_parts(self) -> list[str]:
        return [part for part in self._parts if part != self._ratio.denominator]

    def leaf_metrics(self) -> list[str]:
        return list(dict.fromkeys([self._ratio.numerator, self._ratio.denominator, *self._parts]))

    def check(self, overall: LeafValues) -> None:
        """The parts must sum to the numerator in both periods, then the denominator is checked."""
        numerator = _value(overall, self._ratio.numerator)
        for index, period in ((0, self._labels.base), (1, self._labels.compare)):
            parts_sum = sum(_value(overall, part)[index] for part in self._parts)
            expected = numerator[index]
            scale = max(abs(expected), abs(parts_sum))
            if abs(parts_sum - expected) > self._tolerance * scale:
                raise DecompositionInconsistent(
                    f"metric {self._metric!r}: numerator_parts {self._parts} sum to {parts_sum!r} "
                    f"in {self._labels.dimension} {period}, but the numerator "
                    f"{self._ratio.numerator!r} is {expected!r}"
                )
        self._ratio.check_denominator(overall)

    def total(self, overall: LeafValues) -> Total:
        return self._ratio.total(overall)

    def effects(self, overall: LeafValues) -> list[Effect]:
        avg_inverse, delta_inverse = self._ratio.inverse(overall)
        effects = []
        for part in self._moving_parts:
            values = _value(overall, part)
            effects.append(
                Effect(
                    effect=EffectKind.PART,
                    metric=part,
                    base=values[0],
                    compare=values[1],
                    contribution=_delta(values) * avg_inverse,
                )
            )
        denominator = _value(overall, self._ratio.denominator)
        effects.append(
            Effect(
                effect=EffectKind.DENOMINATOR,
                metric=self._ratio.denominator,
                base=denominator[0],
                compare=denominator[1],
                contribution=sum(_mid(_value(overall, p)) for p in self._moving_parts)
                * delta_inverse,
            )
        )
        return effects

    def split(self, overall: LeafValues, members: MemberValues, top_n: int) -> PartsBreakdown:
        avg_inverse, _ = self._ratio.inverse(overall)
        parts: dict[str, PartBreakdown] = {}
        for part in self._moving_parts:
            rows = _additive_rows(members, part, avg_inverse, top_n)
            effect = _delta(_value(overall, part)) * avg_inverse
            parts[part] = PartBreakdown(rows=rows, residual=effect - _sum(rows))
        denominator_rows = [
            DenominatorRow(member=member, base=d0, compare=d1, delta=d1 - d0)
            for member, values in members.items()
            for d0, d1 in [_value(values, self._ratio.denominator)]
            if d0 or d1
        ]
        ranked = _ranked(denominator_rows, lambda r: r.delta, lambda r: r.member)
        return PartsBreakdown(
            parts=parts,
            denominator=DenominatorBreakdown(rows=_top(ranked, top_n, _merge_denominator)),
        )


def _additive_rows(
    members: MemberValues, metric: str, scale: float, top_n: int
) -> list[AdditiveRow]:
    rows = [
        AdditiveRow(member=member, base=v0, compare=v1, contribution=(v1 - v0) * scale)
        for member, values in members.items()
        for v0, v1 in [_value(values, metric)]
    ]
    ranked = _ranked(rows, lambda r: r.contribution, lambda r: r.member)
    return _top(ranked, top_n, _merge_additive)


def _sum(rows: Sequence[AdditiveRow]) -> float:
    return sum(row.contribution for row in rows)


def _merge_additive(rows: list[AdditiveRow]) -> AdditiveRow:
    return AdditiveRow(
        member=None,
        base=sum(r.base for r in rows),
        compare=sum(r.compare for r in rows),
        contribution=_sum(rows),
        rest=True,
    )


def _merge_shift_share(rows: list[ShiftShareRow]) -> ShiftShareRow:
    return ShiftShareRow(
        member=None,
        share_base=sum(r.share_base for r in rows),
        share_compare=sum(r.share_compare for r in rows),
        rate_base=None,
        rate_compare=None,
        mix=sum(r.mix for r in rows),
        rate=sum(r.rate for r in rows),
        contribution=sum(r.contribution for r in rows),
        presence=None,
        rest=True,
    )


def _merge_denominator(rows: list[DenominatorRow]) -> DenominatorRow:
    return DenominatorRow(
        member=None,
        base=sum(r.base for r in rows),
        compare=sum(r.compare for r in rows),
        delta=sum(r.delta for r in rows),
        rest=True,
    )
