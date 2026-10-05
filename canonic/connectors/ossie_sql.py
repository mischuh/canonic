"""SQL handling for the Ossie connector: dialect selection and metric classification.

Ossie expressions come as ``expression.dialects[]``.  Canonic stores one expression per
measure or dimension, written in the target connection's dialect, so this module picks
the best variant and parses it (AMENDMENT-ossie-interchange §3.4).  It then classifies a
model-level metric into the shapes a source-level measure can carry (§3.5).

Parse failures and expressions present only in non-SQL dialects raise :class:`ValueError`
with a reason. The connector turns that into an "unmappable" warning naming the object.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, cast

import sqlglot
from sqlglot import exp
from sqlglot.errors import SqlglotError

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

__all__ = [
    "Aggregate",
    "AggregateKind",
    "MetricClassifier",
    "Ratio",
    "Unclassified",
    "render",
    "select_expression",
    "strip_qualifiers",
]

# Ossie dialect → sqlglot read dialect. The empty string is sqlglot's generic dialect,
# which covers ANSI SQL and Ossie's portable OSSIE_SQL_2026. MDX, TABLEAU, MAQL, DAX,
# SIGMA and THOUGHTSPOT are not SQL and stay unmappable.
_SQL_DIALECTS: dict[str, str] = {
    "OSSIE_SQL_2026": "",
    "ANSI_SQL": "",
    "SNOWFLAKE": "snowflake",
    "DATABRICKS": "databricks",
    "BIGQUERY": "bigquery",
}
_PORTABLE: tuple[str, ...] = ("OSSIE_SQL_2026", "ANSI_SQL")


def select_expression(
    variants: Sequence[tuple[str, str]], target_dialect: str | None
) -> exp.Expression:
    """Pick and parse the variant to import, in this order of preference.

    1. The variant written in the target connection's own dialect, used as is.
    2. A portable variant (``OSSIE_SQL_2026``, then ``ANSI_SQL``).
    3. Any other SQL dialect sqlglot can read, transpiled on render.

    Raises:
        ValueError: No variant is in a SQL dialect, or the chosen one does not parse.
    """
    by_dialect: dict[str, str] = {}
    for dialect, text in variants:
        by_dialect.setdefault(dialect.upper(), text)
    sql_dialects = [d for d in by_dialect if d in _SQL_DIALECTS]
    native = [d for d in sql_dialects if target_dialect and _SQL_DIALECTS[d] == target_dialect]
    portable = [d for d in _PORTABLE if d in by_dialect]
    others = sorted(d for d in sql_dialects if d not in _PORTABLE)
    ranked = native + portable + others
    if not ranked:
        found = ", ".join(sorted(by_dialect)) or "none"
        raise ValueError(f"no SQL dialect among the expression variants ({found})")
    chosen = ranked[0]
    try:
        parsed = sqlglot.parse_one(by_dialect[chosen], read=_SQL_DIALECTS[chosen] or None)
    except SqlglotError as exc:
        raise ValueError(f"{chosen} expression does not parse: {_first_line(exc)}") from exc
    if parsed is None:
        raise ValueError(f"{chosen} expression is empty")
    return cast("exp.Expression", parsed)


_ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;]*m")


def _first_line(exc: SqlglotError) -> str:
    """sqlglot errors carry terminal highlighting and a multi-line excerpt, keep one plain line."""
    return _ANSI_ESCAPE.sub("", str(exc)).splitlines()[0]


def render(node: exp.Expression, target_dialect: str | None) -> str:
    """Generate SQL for ``node`` in the target dialect (sqlglot's generic one if unknown)."""
    return node.sql(dialect=target_dialect or None)


def strip_qualifiers(node: exp.Expression) -> exp.Expression:
    """Drop dataset qualifiers: a canonic measure lives on one source and names bare columns."""
    stripped = node.copy()
    for column in stripped.find_all(exp.Column):
        for part in ("table", "db", "catalog"):
            column.set(part, None)
    return stripped


class AggregateKind(StrEnum):
    """How a single aggregate behaves when rolled up."""

    ADDITIVE = "additive"
    DISTINCT_COUNT = "distinct_count"
    AVERAGE = "average"


@dataclass(frozen=True)
class Aggregate:
    """One aggregate over columns of a single dataset, qualifiers already stripped."""

    kind: AggregateKind
    dataset: str
    node: exp.Expression


@dataclass(frozen=True)
class Ratio:
    """``numerator / denominator``, each a single-dataset additive or distinct aggregate."""

    numerator: Aggregate
    denominator: Aggregate


@dataclass(frozen=True)
class Unclassified:
    """An expression over one dataset whose additivity cannot be derived."""

    dataset: str
    node: exp.Expression


_ADDITIVE_AGGS: tuple[type[exp.Expression], ...] = (exp.Sum, exp.Count, exp.Min, exp.Max)
_RATIO_COMPONENT_KINDS = frozenset({AggregateKind.ADDITIVE, AggregateKind.DISTINCT_COUNT})


def _unwrap(node: exp.Expression) -> exp.Expression:
    while isinstance(node, exp.Paren):
        node = node.this
    return node


class MetricClassifier:
    """Classifies a metric expression into the shape a measure can carry.

    Args:
        column_owner: Returns the dataset name a column belongs to, or ``None`` when it
            cannot be resolved.
        default_dataset: The dataset an expression without any column (``COUNT(*)``)
            belongs to, known only when the model has exactly one dataset.
    """

    def __init__(
        self,
        column_owner: Callable[[exp.Column], str | None],
        default_dataset: str | None,
    ) -> None:
        self._column_owner = column_owner
        self._default_dataset = default_dataset

    def classify(self, node: exp.Expression) -> Aggregate | Ratio | Unclassified:
        """Classify ``node``.

        Raises:
            ValueError: The expression cannot be placed on exactly one dataset and is not a
                ratio of two classifiable aggregates.
        """
        node = _unwrap(node)
        aggregate = self._aggregate(node)
        if aggregate is not None:
            return aggregate
        if isinstance(node, exp.Div):
            ratio = self._ratio(node)
            if ratio is not None:
                return ratio
        datasets = self._datasets_of(node)
        if not datasets:
            raise ValueError("references no column, so its dataset is ambiguous")
        if len(datasets) != 1:
            raise ValueError(
                f"spans datasets {sorted(datasets)} and is neither a single aggregate nor "
                "a ratio of two"
            )
        return Unclassified(dataset=datasets.pop(), node=strip_qualifiers(node))

    def _ratio(self, node: exp.Div) -> Ratio | None:
        denominator = _unwrap(node.expression)
        if isinstance(denominator, exp.Nullif):
            denominator = _unwrap(denominator.this)
        num = self._aggregate(_unwrap(node.this))
        den = self._aggregate(denominator)
        if num is None or den is None:
            return None
        if num.kind not in _RATIO_COMPONENT_KINDS or den.kind not in _RATIO_COMPONENT_KINDS:
            return None
        return Ratio(numerator=num, denominator=den)

    def _aggregate(self, node: exp.Expression) -> Aggregate | None:
        if isinstance(node, exp.Count) and isinstance(node.this, exp.Distinct):
            kind = AggregateKind.DISTINCT_COUNT
        elif isinstance(node, _ADDITIVE_AGGS):
            kind = AggregateKind.ADDITIVE
        elif isinstance(node, exp.Avg):
            kind = AggregateKind.AVERAGE
        else:
            return None
        datasets = self._datasets_of(node)
        if len(datasets) != 1:
            return None
        return Aggregate(kind=kind, dataset=datasets.pop(), node=strip_qualifiers(node))

    def _datasets_of(self, node: exp.Expression) -> set[str]:
        owners: set[str] = set()
        for column in node.find_all(exp.Column):
            owner = self._column_owner(column)
            if owner is None:
                raise ValueError(f"column {column.sql()!r} belongs to no known dataset")
            owners.add(owner)
        if not owners and self._default_dataset is not None:
            owners.add(self._default_dataset)
        return owners
