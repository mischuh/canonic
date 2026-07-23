"""Multi-metric simple/additive compile path — per-metric filter isolation.

Regression for a bug where compiling several SINGLE-kind metrics together (e.g.
``--metrics total_income,total_expenses,net_cashflow``) returned ``None`` for every
metric, while querying each one individually worked fine. Root cause: every requested
metric's ``population_filter`` and per-measure ``mandatory_filter`` guardrails were
folded into ONE shared WHERE clause covering the single flat SELECT that all metrics
share as sibling projections. When two metrics carry different (e.g. mutually
exclusive) restrictions, the combined WHERE can never match any row, so every
aggregate in that one result row comes back SQL NULL.

Fix: with more than one requested metric, each metric's own population_filter/
guardrail conditions scope only that metric's own aggregate via conditional
aggregation (``SUM(CASE WHEN <condition> THEN <expr> END)``), never a shared WHERE.
A single-metric query is emitted exactly as before (still a plain shared WHERE) —
see the "no-op" and "single-metric shape frozen" tests below.
"""

from __future__ import annotations

import duckdb
import pytest
import sqlglot

from canonic.compiler import SemanticQuery, compile
from canonic.contracts.models import (
    AppliesTo,
    CanonicalRef,
    Guardrail,
    GuardrailKind,
    MetricBinding,
    Severity,
)
from canonic.contracts.resolver import ContractResolver
from canonic.semantic.models import Column, Dimension, Join, Measure, Relationship, SemanticSource


def _parse_ok(sql: str) -> None:
    sqlglot.parse_one(sql, dialect="postgres")


@pytest.fixture
def transactions() -> SemanticSource:
    """Fact table with a type column so population_filter can split income/expense."""
    return SemanticSource(
        name="transactions",
        connection="warehouse_duckdb",
        table="fct_transactions",
        grain=["txn_id"],
        columns=[
            Column(name="txn_id", type="string", nullable=False),
            Column(name="amount", type="decimal", nullable=True),
            Column(name="type", type="string", nullable=False),
        ],
        measures=[Measure(name="amount", expr="sum(amount)", additivity="additive")],
        dimensions=[],
    )


@pytest.fixture
def total_income_binding() -> MetricBinding:
    return MetricBinding(
        metric="total_income",
        canonical=CanonicalRef(
            source="transactions", measure="amount", population_filter="type = 'income'"
        ),
    )


@pytest.fixture
def total_expenses_binding() -> MetricBinding:
    return MetricBinding(
        metric="total_expenses",
        canonical=CanonicalRef(
            source="transactions", measure="amount", population_filter="type = 'expense'"
        ),
    )


@pytest.fixture
def net_cashflow_binding() -> MetricBinding:
    return MetricBinding(
        metric="net_cashflow",
        canonical=CanonicalRef(source="transactions", measure="amount"),
    )


@pytest.fixture
def resolver_multi(
    total_income_binding: MetricBinding,
    total_expenses_binding: MetricBinding,
    net_cashflow_binding: MetricBinding,
) -> ContractResolver:
    return ContractResolver(
        bindings=[total_income_binding, total_expenses_binding, net_cashflow_binding],
        guardrails=[],
    )


# ---------------------------------------------------------------------------
# Direct regression: two conflicting population_filters no longer collide
# ---------------------------------------------------------------------------


def test_two_conflicting_population_filters_dont_collide_structurally(
    resolver_multi: ContractResolver, transactions: SemanticSource
) -> None:
    """No shared WHERE; each metric's filter is scoped via its own CASE WHEN."""
    result = compile(
        SemanticQuery(metrics=["total_income", "total_expenses"]), resolver_multi, [transactions]
    )
    _parse_ok(result.sql)
    sql_upper = result.sql.upper()
    assert "WHERE" not in sql_upper
    assert sql_upper.count("CASE WHEN") == 2
    assert "'income'" in result.sql
    assert "'expense'" in result.sql


def test_population_filter_mixed_with_metric_without_filter(
    resolver_multi: ContractResolver, transactions: SemanticSource
) -> None:
    """Three metrics, only two carry a filter: exactly 2 CASE WHEN, third stays plain."""
    result = compile(
        SemanticQuery(metrics=["total_income", "total_expenses", "net_cashflow"]),
        resolver_multi,
        [transactions],
    )
    _parse_ok(result.sql)
    assert result.sql.upper().count("CASE WHEN") == 2
    assert result.sql.upper().count("SUM(") == 3


def test_multi_metric_execution_no_longer_returns_none(
    resolver_multi: ContractResolver, transactions: SemanticSource
) -> None:
    """The falsifying test: execute the compiled SQL against real data — no NULLs."""
    result = compile(
        SemanticQuery(metrics=["total_income", "total_expenses", "net_cashflow"]),
        resolver_multi,
        [transactions],
        connection_dialects={"warehouse_duckdb": "duckdb"},
    )
    con = duckdb.connect(":memory:")
    con.execute("CREATE TABLE fct_transactions (txn_id VARCHAR, amount DECIMAL, type VARCHAR)")
    con.execute(
        "INSERT INTO fct_transactions VALUES "
        "('1', 100, 'income'), ('2', 50, 'income'), ('3', -30, 'expense'), ('4', -20, 'expense')"
    )
    row = con.execute(result.sql).fetchone()
    assert row is not None
    total_income, total_expenses, net_cashflow = row
    assert None not in row
    assert total_income == 150
    assert total_expenses == -50
    assert net_cashflow == 100


# ---------------------------------------------------------------------------
# Guardrail mandatory_filter suffers (and is fixed for) the same collision
# ---------------------------------------------------------------------------


def test_guardrail_mandatory_filter_scoped_per_metric() -> None:
    """Two measures, each with a mutually exclusive mandatory_filter guardrail.

    Guardrails key on (source, measure), so each metric needs its own measure to
    keep the two guardrails independent.
    """
    expense_source = SemanticSource(
        name="transactions",
        connection="warehouse_duckdb",
        table="fct_transactions",
        grain=["txn_id"],
        columns=[
            Column(name="txn_id", type="string", nullable=False),
            Column(name="amount", type="decimal", nullable=True),
            Column(name="type", type="string", nullable=False),
        ],
        measures=[
            Measure(name="income_amount", expr="sum(amount)", additivity="additive"),
            Measure(name="expense_amount", expr="sum(amount)", additivity="additive"),
        ],
        dimensions=[],
    )
    income_binding = MetricBinding(
        metric="total_income",
        canonical=CanonicalRef(source="transactions", measure="income_amount"),
    )
    expense_binding = MetricBinding(
        metric="total_expenses",
        canonical=CanonicalRef(source="transactions", measure="expense_amount"),
    )
    income_guardrail = Guardrail(
        id="income-only",
        applies_to=AppliesTo(source="transactions", measure="income_amount"),
        kind=GuardrailKind.MANDATORY_FILTER,
        filter="type = 'income'",
        severity=Severity.ERROR,
        rationale="income_amount must only ever see income rows.",
    )
    expense_guardrail = Guardrail(
        id="expense-only",
        applies_to=AppliesTo(source="transactions", measure="expense_amount"),
        kind=GuardrailKind.MANDATORY_FILTER,
        filter="type = 'expense'",
        severity=Severity.ERROR,
        rationale="expense_amount must only ever see expense rows.",
    )
    resolver = ContractResolver(
        bindings=[income_binding, expense_binding],
        guardrails=[income_guardrail, expense_guardrail],
    )
    result = compile(
        SemanticQuery(metrics=["total_income", "total_expenses"]), resolver, [expense_source]
    )
    _parse_ok(result.sql)
    sql_upper = result.sql.upper()
    assert "WHERE" not in sql_upper
    assert sql_upper.count("CASE WHEN") == 2
    assert {g.id for g in result.guardrails_fired} == {"income-only", "expense-only"}


# ---------------------------------------------------------------------------
# No-op regression guard: no filters/guardrails at all → unchanged SQL shape
# ---------------------------------------------------------------------------


def test_no_filters_multi_metric_unchanged_shape(transactions: SemanticSource) -> None:
    """Two plain metrics with no population_filter/guardrails: no CASE WHEN, no WHERE."""
    b1 = MetricBinding(
        metric="metric_a", canonical=CanonicalRef(source="transactions", measure="amount")
    )
    b2 = MetricBinding(
        metric="metric_b", canonical=CanonicalRef(source="transactions", measure="amount")
    )
    resolver = ContractResolver(bindings=[b1, b2], guardrails=[])
    result = compile(SemanticQuery(metrics=["metric_a", "metric_b"]), resolver, [transactions])
    _parse_ok(result.sql)
    sql_upper = result.sql.upper()
    assert "CASE WHEN" not in sql_upper
    assert "WHERE" not in sql_upper
    assert sql_upper.count("SUM(") == 2


# ---------------------------------------------------------------------------
# Fanout / dedup path also gets per-metric conditional aggregation
# ---------------------------------------------------------------------------


@pytest.fixture
def transactions_with_fanout() -> SemanticSource:
    """Same transactions source, joined one_to_many to a child table to force dedup."""
    return SemanticSource(
        name="transactions",
        connection="warehouse_duckdb",
        table="fct_transactions",
        grain=["txn_id"],
        columns=[
            Column(name="txn_id", type="string", nullable=False),
            Column(name="amount", type="decimal", nullable=True),
            Column(name="type", type="string", nullable=False),
        ],
        measures=[Measure(name="amount", expr="sum(amount)", additivity="additive")],
        dimensions=[],
        joins=[
            Join(
                to="transaction_tags",
                on="transactions.txn_id = transaction_tags.txn_id",
                relationship=Relationship.ONE_TO_MANY,
            )
        ],
    )


@pytest.fixture
def transaction_tags() -> SemanticSource:
    return SemanticSource(
        name="transaction_tags",
        connection="warehouse_duckdb",
        table="fct_transaction_tags",
        grain=["tag_id"],
        columns=[
            Column(name="tag_id", type="string", nullable=False),
            Column(name="txn_id", type="string", nullable=False),
            Column(name="tag", type="string", nullable=False),
        ],
        dimensions=[Dimension(name="tag", column="tag")],
    )


def test_fanout_dedup_path_conditional_aggregation(
    resolver_multi: ContractResolver,
    transactions_with_fanout: SemanticSource,
    transaction_tags: SemanticSource,
) -> None:
    """A one_to_many join forces _build_deduped; per-metric filters still isolate correctly."""
    result = compile(
        SemanticQuery(metrics=["total_income", "total_expenses"], dimensions=["tag"]),
        resolver_multi,
        [transactions_with_fanout, transaction_tags],
    )
    _parse_ok(result.sql)
    sql_upper = result.sql.upper()
    assert "DISTINCT ON" in sql_upper
    assert sql_upper.count("CASE WHEN") == 2
    # The filter's source column must be projected by the inner dedup subquery.
    assert '"type"' in result.sql.lower() or "type" in result.sql.lower()
