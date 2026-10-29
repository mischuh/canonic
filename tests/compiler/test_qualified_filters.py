"""Filters and typed time ranges on role-qualified dimensions (``EventDate.month_start``).

A qualified dimension name is a semantic name, not a physical column. In a filter it resolves
like a bare dimension name: it binds to the backing column (or inlines its expression) and
compares the underlying value, not the bucket SELECT and GROUP BY show.
"""

from __future__ import annotations

import pytest
import sqlglot
from sqlglot import exp

from canonic.compiler import SemanticQuery, compile
from canonic.contracts.models import (
    AllowDenyPolicy,
    CanonicalRef,
    MetricBinding,
    RoleDef,
    RolePolicy,
)
from canonic.contracts.principal import Principal
from canonic.contracts.resolver import ContractResolver
from canonic.exc import Unreachable
from canonic.semantic.models import (
    Column,
    Dimension,
    Join,
    Measure,
    Relationship,
    SemanticSource,
)


@pytest.fixture
def dim_date() -> SemanticSource:
    return SemanticSource(
        name="dim_date",
        connection="wh",
        table="iv.t_dim_date",
        grain=["dim_date_key"],
        columns=[
            Column(name="dim_date_key", type="string", nullable=False),
            Column(name="day_actual", type="date", nullable=False),
            Column(name="epoch", type="int", nullable=False),
        ],
        dimensions=[
            Dimension(name="month_start", column="day_actual", granularity="month"),
            Dimension(name="day", column="day_actual", granularity="day"),
            Dimension(
                name="event_ts", expr="to_timestamp(epoch)", type="timestamp", granularity="day"
            ),
        ],
    )


@pytest.fixture
def kpis() -> SemanticSource:
    return SemanticSource(
        name="kpis",
        connection="wh",
        table="mart.t_kpis",
        grain=["id"],
        columns=[
            Column(name="id", type="string", nullable=False),
            Column(name="merchant_id", type="int", nullable=False),
            Column(name="dim_date_key", type="string", nullable=False),
            Column(name="cohort_date_key", type="string", nullable=False),
            Column(name="num_payments", type="int", nullable=False),
        ],
        measures=[Measure(name="payments", expr="sum(num_payments)")],
        joins=[
            Join(
                name="EventDate",
                to="dim_date",
                on="kpis.dim_date_key = dim_date.dim_date_key",
                relationship=Relationship.MANY_TO_ONE,
            ),
            Join(
                name="CohortDate",
                to="dim_date",
                on="kpis.cohort_date_key = dim_date.dim_date_key",
                relationship=Relationship.MANY_TO_ONE,
            ),
        ],
    )


@pytest.fixture
def resolver() -> ContractResolver:
    binding = MetricBinding(
        metric="payments", canonical=CanonicalRef(source="kpis", measure="payments")
    )
    return ContractResolver(bindings=[binding], guardrails=[])


def _sql(resolver, sources, **query) -> str:
    return compile(SemanticQuery(metrics=["payments"], **query), resolver, sources).sql


def _where(sql: str) -> str:
    parsed = sqlglot.parse_one(sql, dialect="postgres")
    assert isinstance(parsed, exp.Select)
    return parsed.args["where"].sql(dialect="postgres")


def test_filter_on_qualified_dimension_binds_the_backing_column(resolver, kpis, dim_date) -> None:
    sql = _sql(
        resolver,
        [kpis, dim_date],
        dimensions=["EventDate.month_start"],
        filters=["EventDate.month_start >= '2026-04-01'", "EventDate.month_start < '2026-10-01'"],
    )
    where = _where(sql)
    assert '"EventDate"."day_actual" >= \'2026-04-01\'' in where
    assert '"EventDate"."day_actual" < \'2026-10-01\'' in where
    assert '"EventDate"."month_start"' not in where
    assert "DATE_TRUNC" not in where
    assert 'DATE_TRUNC(\'MONTH\', "EventDate"."day_actual")' in sql


def test_filter_on_qualified_dimension_joins_a_filter_only_role(resolver, kpis, dim_date) -> None:
    sql = _sql(resolver, [kpis, dim_date], filters=["EventDate.month_start >= '2026-04-01'"])
    assert '"EventDate"."day_actual"' in _where(sql)
    assert 'AS "EventDate"' in sql
    assert 'AS "CohortDate"' not in sql


def test_filter_on_qualified_dimension_keeps_roles_apart(resolver, kpis, dim_date) -> None:
    sql = _sql(
        resolver,
        [kpis, dim_date],
        filters=["EventDate.month_start >= '2026-04-01'", "CohortDate.day < '2026-01-01'"],
    )
    where = _where(sql)
    assert '"EventDate"."day_actual" >= \'2026-04-01\'' in where
    assert '"CohortDate"."day_actual" < \'2026-01-01\'' in where


def test_filter_on_qualified_expr_dimension_inlines_the_expression(
    resolver, kpis, dim_date
) -> None:
    sql = _sql(resolver, [kpis, dim_date], filters=["EventDate.event_ts >= '2026-04-01'"])
    where = _where(sql).upper()
    assert "TO_TIMESTAMP" in where
    assert '"EVENTDATE"."EPOCH"' in where
    assert "DATE_TRUNC" not in where


def test_filter_on_non_aligned_bound_compares_the_underlying_column(
    resolver, kpis, dim_date
) -> None:
    """A bucketed dimension filters on its column, so a mid-month bound stays mid-month."""
    sql = _sql(
        resolver,
        [kpis, dim_date],
        dimensions=["EventDate.month_start"],
        filters=["EventDate.month_start >= '2026-04-15'"],
    )
    assert '"EventDate"."day_actual" >= \'2026-04-15\'' in _where(sql)


def test_filter_on_qualified_physical_column_still_compiles(resolver, kpis, dim_date) -> None:
    sql = _sql(resolver, [kpis, dim_date], filters=["EventDate.day_actual >= '2026-04-01'"])
    assert '"EventDate"."day_actual" >= \'2026-04-01\'' in _where(sql)


def test_unqualified_dimension_filter_is_unchanged(resolver, kpis, dim_date) -> None:
    sql = _sql(
        resolver,
        [kpis, dim_date],
        dimensions=["EventDate.day"],
        filters=["merchant_id = 725"],
    )
    assert '"kpis"."merchant_id" = 725' in _where(sql)


@pytest.mark.parametrize(
    "time_range",
    [
        {"dimension": "EventDate.month_start", "start": "2026-04-01", "end": "2026-10-01"},
        {"dimension": "EventDate.month_start", "last": "3 months"},
    ],
)
def test_time_range_on_qualified_dimension_binds_the_backing_column(
    resolver, kpis, dim_date, time_range
) -> None:
    sql = _sql(
        resolver, [kpis, dim_date], dimensions=["EventDate.month_start"], time_range=time_range
    )
    where = _where(sql)
    assert '"EventDate"."day_actual" >=' in where
    assert '"EventDate"."day_actual" <' in where
    assert '"EventDate"."month_start"' not in where


@pytest.mark.parametrize(
    "filter_",
    [
        "Warehouse.month_start >= '2026-04-01'",
        "EventDate.no_such_name >= '2026-04-01'",
    ],
)
def test_unknown_qualified_reference_is_unreachable(resolver, kpis, dim_date, filter_) -> None:
    with pytest.raises(Unreachable):
        _sql(resolver, [kpis, dim_date], filters=[filter_])


def test_unknown_qualified_time_range_dimension_is_unreachable(resolver, kpis, dim_date) -> None:
    with pytest.raises(Unreachable):
        _sql(
            resolver,
            [kpis, dim_date],
            time_range={"dimension": "Warehouse.month_start", "last": "3 months"},
        )


def test_filter_on_denied_qualified_dimension_is_refused(kpis, dim_date) -> None:
    binding = MetricBinding(
        metric="payments", canonical=CanonicalRef(source="kpis", measure="payments")
    )
    policy = RolePolicy(
        schema_="roles/v1",
        claim="roles",
        default_role="restricted",
        roles={
            "restricted": RoleDef(
                metrics=AllowDenyPolicy(allow=["*"]),
                dimensions=AllowDenyPolicy(allow=["*"], deny=["month_start"]),
            )
        },
    )
    resolver = ContractResolver(bindings=[binding], guardrails=[], roles=policy)
    with pytest.raises(Unreachable):
        compile(
            SemanticQuery(metrics=["payments"], filters=["EventDate.month_start >= '2026-04-01'"]),
            resolver,
            [kpis, dim_date],
            principal=Principal(roles=("restricted",)),
        )
