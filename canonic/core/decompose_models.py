"""Request and response models of the ``decompose`` capability.

``decompose`` attributes the change of one metric between two periods of one time dimension,
overall and by member of one or more split dimensions. The models are transport-neutral: MCP and
CLI serialize them verbatim, like :class:`~canonic.core.models.QueryResult`.
"""

from __future__ import annotations

from datetime import datetime  # noqa: TC003 — Pydantic resolves field annotations at runtime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from canonic.compiler.query import coerce_filters
from canonic.core.models import (
    QueryMetadata,  # noqa: TC001 — Pydantic resolves field annotations at runtime
)

__all__ = [
    "AdditiveBreakdown",
    "AdditiveRow",
    "DecomposeMethodName",
    "DecomposePeriod",
    "DecomposeRequest",
    "DecomposeResult",
    "DenominatorBreakdown",
    "DenominatorRow",
    "Effect",
    "EffectKind",
    "PartBreakdown",
    "PartsBreakdown",
    "Presence",
    "ShiftShareBreakdown",
    "ShiftShareRow",
    "Total",
]


class DecomposeMethodName(StrEnum):
    """How the change was attributed, chosen by the binding kind."""

    ADDITIVE = "additive"
    RATIO_SHIFT_SHARE = "ratio_shift_share"
    RATIO_PARTS = "ratio_parts"


class EffectKind(StrEnum):
    """One overall effect of a ratio change."""

    NUMERATOR = "numerator"
    DENOMINATOR = "denominator"
    PART = "part"


class Presence(StrEnum):
    """In which of the two periods a member has a denominator."""

    BOTH = "both"
    BASE_ONLY = "base_only"
    COMPARE_ONLY = "compare_only"


class DecomposePeriod(BaseModel):
    """The two compared values of one time dimension. Their order only sets the sign."""

    model_config = ConfigDict(frozen=True)

    dimension: str
    base: str | int
    compare: str | int


class DecomposeRequest(BaseModel):
    """A request to attribute the change of ``metric`` from ``period.base`` to ``period.compare``.

    ``filters``, ``via``, ``context`` and ``as_of`` mean what they mean in a
    :class:`~canonic.compiler.query.SemanticQuery` and apply to both periods. ``top_n`` defaults
    to ``decompose.top_n`` and is capped at ``decompose.max_top_n``.
    """

    model_config = ConfigDict(frozen=True)

    metric: str
    period: DecomposePeriod
    by: list[str] = []
    filters: list[str] = []
    via: list[str] = []
    context: str | None = None
    as_of: datetime | None = None
    top_n: int | None = Field(default=None, ge=1)
    allow_open_period: bool = False

    @field_validator("filters", mode="before")
    @classmethod
    def _coerce_filters(cls, v: Any) -> list[str]:
        return coerce_filters(v)


class Total(BaseModel):
    """The metric in both periods and its change."""

    model_config = ConfigDict(frozen=True)

    base: float
    compare: float
    delta: float


class Effect(BaseModel):
    """One overall effect of a ratio change, in the metric's units.

    ``metric`` is the numerator, the denominator or the part the effect belongs to, and
    ``base``/``compare`` are that metric's own values.
    """

    model_config = ConfigDict(frozen=True)

    effect: EffectKind
    metric: str
    base: float
    compare: float
    contribution: float


class AdditiveRow(BaseModel):
    """One member's values and its contribution to the change.

    A row with ``rest: true`` merges every member beyond ``top_n``. Its ``member`` is ``null``.
    """

    model_config = ConfigDict(frozen=True)

    member: Any
    base: float
    compare: float
    contribution: float
    rest: bool = False


class ShiftShareRow(BaseModel):
    """One member of a ratio split by shift-share.

    ``share`` is the member's part of the denominator and ``rate`` its own ratio. ``mix`` is the
    contribution from the share moving, ``rate`` (the effect) from the member's ratio moving.
    A member without a denominator in one period has no rate there (``null``), its whole term
    counts as mix, and ``presence`` says which period it is in. A rest row carries the summed
    shares and effects, but no rates and no presence.
    """

    model_config = ConfigDict(frozen=True)

    member: Any
    share_base: float
    share_compare: float
    rate_base: float | None
    rate_compare: float | None
    mix: float
    rate: float
    contribution: float
    presence: Presence | None
    rest: bool = False


class DenominatorRow(BaseModel):
    """Where the denominator moved, in the denominator's own units."""

    model_config = ConfigDict(frozen=True)

    member: Any
    base: float
    compare: float
    delta: float
    rest: bool = False


class AdditiveBreakdown(BaseModel):
    """The change split by the members of one dimension. ``residual`` is what the rows miss."""

    model_config = ConfigDict(frozen=True)

    rows: list[AdditiveRow]
    residual: float


class ShiftShareBreakdown(BaseModel):
    """A ratio change split by the members of one dimension into mix and rate."""

    model_config = ConfigDict(frozen=True)

    rows: list[ShiftShareRow]
    residual: float


class PartBreakdown(BaseModel):
    """One numerator part's effect split by member. ``residual`` is against the part effect."""

    model_config = ConfigDict(frozen=True)

    rows: list[AdditiveRow]
    residual: float


class DenominatorBreakdown(BaseModel):
    """The denominator by member. These rows do not sum to the metric change."""

    model_config = ConfigDict(frozen=True)

    units: Literal["denominator"] = "denominator"
    rows: list[DenominatorRow]


class PartsBreakdown(BaseModel):
    """A ratio change with numerator parts, split by the members of one dimension."""

    model_config = ConfigDict(frozen=True)

    parts: dict[str, PartBreakdown]
    denominator: DenominatorBreakdown


class DecomposeResult(BaseModel):
    """The response of ``decompose``: the change, its effects and its split by member.

    ``effects`` is empty for ``additive``. ``by`` has one entry per requested split dimension,
    in request order, shaped by ``method``. ``metadata`` merges the metadata of every statement
    the call ran.
    """

    model_config = ConfigDict(frozen=True)

    metric: str
    kind: str
    method: DecomposeMethodName
    period: DecomposePeriod
    total: Total
    effects: list[Effect] = []
    by: dict[str, AdditiveBreakdown | ShiftShareBreakdown | PartsBreakdown] = {}
    metadata: QueryMetadata
