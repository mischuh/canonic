"""Compiler tests for partial_additive strategy — semi_additive (GH-119, S3).

Acceptance criteria:
  AC1: ending_inventory (collapse: snapshot_date, last) grouped by warehouse only →
       balances sum across warehouses but take the last snapshot over time (never summed
       over time). SQL uses ROW_NUMBER() OVER (...) window; result.partial_additive.collapsed.
  AC2: grouped by snapshot_date → behaves additively; plain SUM; collapsed=False.
"""

from __future__ import annotations

import pytest
import sqlglot

from canonic import exc
from canonic.compiler import SemanticQuery, compile
from canonic.contracts.models import BindingKind, CanonicalRef, CollapseAgg, MetricBinding
from canonic.contracts.resolver import ContractResolver
from canonic.semantic.models import Column, Dimension, Join, Measure, Relationship, SemanticSource

# ---------------------------------------------------------------------------
# Fixtures — in-memory inventory_snapshots project
# ---------------------------------------------------------------------------


@pytest.fixture
def inventory_source() -> SemanticSource:
    """Daily snapshot: additive inventory_level, collapse over snapshot_date."""
    return SemanticSource(
        name="inventory_snapshots",
        connection="warehouse_pg",
        table="analytics.inventory_snapshots",
        grain=["warehouse_id", "snapshot_date"],
        columns=[
            Column(name="warehouse_id", type="string", nullable=False),
            Column(name="snapshot_date", type="date", nullable=False),
            Column(name="inventory_level", type="decimal", nullable=False),
        ],
        measures=[
            Measure(
                name="inventory_level",
                expr="sum(inventory_level)",
                additivity="additive",
            )
        ],
        dimensions=[
            Dimension(name="warehouse_id", column="warehouse_id"),
            Dimension(name="snapshot_date", column="snapshot_date", granularity="day"),
        ],
    )


@pytest.fixture
def fanning_source() -> SemanticSource:
    """A one_to_many join target to test fanout rejection."""
    return SemanticSource(
        name="sku_tags",
        connection="warehouse_pg",
        table="analytics.sku_tags",
        grain=["warehouse_id", "tag"],
        columns=[
            Column(name="warehouse_id", type="string", nullable=False),
            Column(name="tag", type="string", nullable=False),
        ],
        dimensions=[Dimension(name="tag", column="tag")],
    )


@pytest.fixture
def inventory_source_with_join(
    inventory_source: SemanticSource, fanning_source: SemanticSource
) -> SemanticSource:
    """inventory_snapshots with a one_to_many join to sku_tags."""
    return SemanticSource(
        name=inventory_source.name,
        connection=inventory_source.connection,
        table=inventory_source.table,
        grain=inventory_source.grain,
        columns=inventory_source.columns,
        measures=inventory_source.measures,
        dimensions=inventory_source.dimensions,
        joins=[
            Join(
                to="sku_tags",
                on="inventory_snapshots.warehouse_id = sku_tags.warehouse_id",
                relationship=Relationship.ONE_TO_MANY,
            )
        ],
    )


@pytest.fixture
def ending_inventory_last(inventory_source: SemanticSource) -> MetricBinding:
    return MetricBinding(
        metric="ending_inventory",
        canonical=CanonicalRef(
            kind=BindingKind.SEMI_ADDITIVE,
            source="inventory_snapshots",
            measure="inventory_level",
            collapse_dimension="snapshot_date",
            collapse_agg=CollapseAgg.LAST,
        ),
        aliases=["fleet size"],
    )


@pytest.fixture
def resolver(ending_inventory_last: MetricBinding) -> ContractResolver:
    return ContractResolver(bindings=[ending_inventory_last], guardrails=[])


@pytest.fixture
def additive_binding() -> MetricBinding:
    """A plain additive metric to test mixed-metric rejection."""
    return MetricBinding(
        metric="total_inventory",
        canonical=CanonicalRef(source="inventory_snapshots", measure="inventory_level"),
    )


def _parse_ok(sql: str) -> None:
    sqlglot.parse_one(sql, dialect="postgres")


# ---------------------------------------------------------------------------
# AC1 — collapsed: window form, take the last snapshot
# ---------------------------------------------------------------------------


def test_ac1_collapsed_uses_row_number(
    resolver: ContractResolver, inventory_source: SemanticSource
) -> None:
    """Grouped by warehouse only → ROW_NUMBER window; rn = 1 filter; SUM in outer SELECT."""
    result = compile(
        SemanticQuery(metrics=["ending_inventory"], dimensions=["warehouse_id"]),
        resolver,
        [inventory_source],
    )
    _parse_ok(result.sql)
    sql_upper = result.sql.upper()
    assert "ROW_NUMBER()" in sql_upper
    assert "PARTITION BY" in sql_upper
    assert "ORDER BY" in sql_upper
    assert "DESC" in sql_upper
    assert "SUM(" in sql_upper
    assert result.partial_additive is not None
    assert result.partial_additive.collapsed is True
    assert result.partial_additive.collapse_dimension == "snapshot_date"
    assert result.partial_additive.collapse_agg == "last"
    assert result.partial_additive.kind == "semi_additive"


def test_ac1_scalar_no_dims(resolver: ContractResolver, inventory_source: SemanticSource) -> None:
    """No dimensions: still partition by the source grain (warehouse_id), sum the last
    snapshot per warehouse — not a single arbitrary row across the whole table (GH-119
    regression: an empty PARTITION BY let ROW_NUMBER() rank the entire table, so rn = 1
    matched only one of several tied-latest rows instead of one per entity).
    """
    result = compile(
        SemanticQuery(metrics=["ending_inventory"]),
        resolver,
        [inventory_source],
    )
    _parse_ok(result.sql)
    sql_upper = result.sql.upper()
    assert "ROW_NUMBER()" in sql_upper
    assert "PARTITION BY" in sql_upper
    assert '"WAREHOUSE_ID"' in sql_upper
    assert result.partial_additive is not None
    assert result.partial_additive.collapsed is True


def test_ac1_scalar_no_dims_sums_across_entities(resolver: ContractResolver) -> None:
    """Regression: scalar query over a multi-entity snapshot table must sum the latest
    value per entity, not collapse to a single row (GH-119).
    """
    import duckdb

    source = SemanticSource(
        name="inventory_snapshots",
        connection="warehouse_pg",
        table="inventory_snapshots",
        grain=["warehouse_id", "snapshot_date"],
        columns=[
            Column(name="warehouse_id", type="string", nullable=False),
            Column(name="snapshot_date", type="date", nullable=False),
            Column(name="inventory_level", type="int", nullable=False),
        ],
        measures=[
            Measure(name="inventory_level", expr="sum(inventory_level)", additivity="additive")
        ],
        dimensions=[
            Dimension(name="warehouse_id", column="warehouse_id"),
            Dimension(name="snapshot_date", column="snapshot_date", granularity="day"),
        ],
    )
    result = compile(
        SemanticQuery(metrics=["ending_inventory"]),
        resolver,
        [source],
    )

    con = duckdb.connect()
    con.execute(
        "CREATE TABLE inventory_snapshots (warehouse_id TEXT, snapshot_date DATE, inventory_level INT)"
    )
    con.execute(
        "INSERT INTO inventory_snapshots VALUES "
        "('w1', '2024-01-01', 10), ('w2', '2024-01-01', 20), "
        "('w1', '2024-02-01', 5),  ('w2', '2024-02-01', 8)"
    )
    sql = result.sql.replace('"inventory_snapshots"', "inventory_snapshots")
    rows = con.execute(sql).fetchall()
    assert rows == [(13,)]  # last-per-warehouse: w1=5 + w2=8, not a single tied row


def test_ac1_resolved_by_alias(
    resolver: ContractResolver, inventory_source: SemanticSource
) -> None:
    """Semi-additive metric resolves when queried by alias."""
    result = compile(
        SemanticQuery(metrics=["fleet size"], dimensions=["warehouse_id"]),
        resolver,
        [inventory_source],
    )
    _parse_ok(result.sql)
    assert "fleet size" in result.resolved


# ---------------------------------------------------------------------------
# AC2 — additive: grouped by collapse dimension, plain SUM
# ---------------------------------------------------------------------------


def test_ac2_grouped_by_collapse_dim_is_additive(
    resolver: ContractResolver, inventory_source: SemanticSource
) -> None:
    """Grouped by snapshot_date → plain SUM; no ROW_NUMBER; collapsed=False."""
    result = compile(
        SemanticQuery(metrics=["ending_inventory"], dimensions=["snapshot_date"]),
        resolver,
        [inventory_source],
    )
    _parse_ok(result.sql)
    sql_upper = result.sql.upper()
    assert "SUM(" in sql_upper
    assert "ROW_NUMBER()" not in sql_upper
    assert result.partial_additive is not None
    assert result.partial_additive.collapsed is False


def test_ac2_grouped_by_collapse_and_other(
    resolver: ContractResolver, inventory_source: SemanticSource
) -> None:
    """Grouped by both warehouse and snapshot_date → additive (collapse_dim is grouped)."""
    result = compile(
        SemanticQuery(metrics=["ending_inventory"], dimensions=["warehouse_id", "snapshot_date"]),
        resolver,
        [inventory_source],
    )
    _parse_ok(result.sql)
    assert "ROW_NUMBER()" not in result.sql.upper()
    assert result.partial_additive is not None
    assert result.partial_additive.collapsed is False


# ---------------------------------------------------------------------------
# collapse_agg variants
# ---------------------------------------------------------------------------


def _make_resolver(collapse_agg: CollapseAgg, inventory_source: SemanticSource) -> ContractResolver:
    binding = MetricBinding(
        metric="ending_inventory",
        canonical=CanonicalRef(
            kind=BindingKind.SEMI_ADDITIVE,
            source="inventory_snapshots",
            measure="inventory_level",
            collapse_dimension="snapshot_date",
            collapse_agg=collapse_agg,
        ),
    )
    return ContractResolver(bindings=[binding], guardrails=[])


def test_collapse_agg_first_uses_asc(inventory_source: SemanticSource) -> None:
    """collapse_agg=first → ROW_NUMBER ordered ASC."""
    r = _make_resolver(CollapseAgg.FIRST, inventory_source)
    result = compile(
        SemanticQuery(metrics=["ending_inventory"], dimensions=["warehouse_id"]),
        r,
        [inventory_source],
    )
    _parse_ok(result.sql)
    sql_upper = result.sql.upper()
    assert "ROW_NUMBER()" in sql_upper
    assert "ASC" in sql_upper
    assert "DESC" not in sql_upper
    assert result.partial_additive is not None
    assert result.partial_additive.collapse_agg == "first"


def test_collapse_agg_avg_uses_nested_group_by(inventory_source: SemanticSource) -> None:
    """collapse_agg=avg → nested per_snapshot CTE with outer AVG(m)."""
    r = _make_resolver(CollapseAgg.AVG, inventory_source)
    result = compile(
        SemanticQuery(metrics=["ending_inventory"], dimensions=["warehouse_id"]),
        r,
        [inventory_source],
    )
    _parse_ok(result.sql)
    sql_upper = result.sql.upper()
    assert "ROW_NUMBER()" not in sql_upper
    assert "AVG(" in sql_upper
    assert result.partial_additive is not None
    assert result.partial_additive.collapse_agg == "avg"


def test_collapse_agg_min(inventory_source: SemanticSource) -> None:
    """collapse_agg=min → nested CTE with outer MIN(m)."""
    r = _make_resolver(CollapseAgg.MIN, inventory_source)
    result = compile(
        SemanticQuery(metrics=["ending_inventory"], dimensions=["warehouse_id"]),
        r,
        [inventory_source],
    )
    _parse_ok(result.sql)
    assert "MIN(" in result.sql.upper()
    assert "ROW_NUMBER()" not in result.sql.upper()


def test_collapse_agg_max(inventory_source: SemanticSource) -> None:
    """collapse_agg=max → nested CTE with outer MAX(m)."""
    r = _make_resolver(CollapseAgg.MAX, inventory_source)
    result = compile(
        SemanticQuery(metrics=["ending_inventory"], dimensions=["warehouse_id"]),
        r,
        [inventory_source],
    )
    _parse_ok(result.sql)
    assert "MAX(" in result.sql.upper()
    assert "ROW_NUMBER()" not in result.sql.upper()


# ---------------------------------------------------------------------------
# Safety floor
# ---------------------------------------------------------------------------


def test_fanout_join_raises_fanout_unsafe(
    ending_inventory_last: MetricBinding,
    inventory_source_with_join: SemanticSource,
    fanning_source: SemanticSource,
) -> None:
    """Semi-additive + one_to_many join → FanoutUnsafe."""
    r = ContractResolver(bindings=[ending_inventory_last], guardrails=[])
    with pytest.raises(exc.FanoutUnsafe):
        compile(
            SemanticQuery(metrics=["ending_inventory"], dimensions=["warehouse_id", "tag"]),
            r,
            [inventory_source_with_join, fanning_source],
        )


def test_mixed_with_other_metric_raises(
    ending_inventory_last: MetricBinding,
    additive_binding: MetricBinding,
    inventory_source: SemanticSource,
) -> None:
    """Semi-additive metric queried alongside another metric → UnsupportedMeasure."""
    r = ContractResolver(bindings=[ending_inventory_last, additive_binding], guardrails=[])
    with pytest.raises(exc.UnsupportedMeasure, match="alone"):
        compile(
            SemanticQuery(metrics=["ending_inventory", "total_inventory"]),
            r,
            [inventory_source],
        )


# ---------------------------------------------------------------------------
# Schema validation
# ---------------------------------------------------------------------------


def test_semi_additive_missing_collapse_dimension_raises() -> None:
    """semi_additive binding without collapse_dimension raises ValidationError."""
    from pydantic import ValidationError

    with pytest.raises(ValidationError, match="collapse_dimension"):
        CanonicalRef(
            kind=BindingKind.SEMI_ADDITIVE,
            source="src",
            measure="m",
            collapse_agg=CollapseAgg.LAST,
        )


def test_semi_additive_missing_collapse_agg_raises() -> None:
    """semi_additive binding without collapse_agg raises ValidationError."""
    from pydantic import ValidationError

    with pytest.raises(ValidationError, match="collapse_agg"):
        CanonicalRef(
            kind=BindingKind.SEMI_ADDITIVE,
            source="src",
            measure="m",
            collapse_dimension="snapshot_date",
        )


def test_semi_additive_missing_source_raises() -> None:
    """semi_additive binding without source raises ValidationError."""
    from pydantic import ValidationError

    with pytest.raises(ValidationError, match="source"):
        CanonicalRef(
            kind=BindingKind.SEMI_ADDITIVE,
            measure="m",
            collapse_dimension="snapshot_date",
            collapse_agg=CollapseAgg.LAST,
        )


def test_collapse_agg_yaml_roundtrip() -> None:
    """collapse_agg: last in YAML parses to CollapseAgg.LAST."""
    ref = CanonicalRef.model_validate(
        {
            "kind": "semi_additive",
            "source": "src",
            "measure": "m",
            "collapse_dimension": "snapshot_date",
            "collapse_agg": "last",
        }
    )
    assert ref.collapse_agg is CollapseAgg.LAST


# ---------------------------------------------------------------------------
# Contract validation (cross-surface checks via validate_contracts)
# ---------------------------------------------------------------------------


def test_validate_rejects_non_additive_base_measure() -> None:
    """validate_contracts rejects semi_additive binding whose base measure is not additive."""
    import tempfile
    from pathlib import Path

    from canonic.contracts.validate import validate_contracts
    from canonic.exc import ContractError

    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        (root / "contracts" / "metrics").mkdir(parents=True)
        (root / "semantics" / "db").mkdir(parents=True)

        (root / "semantics" / "db" / "src.yaml").write_text(
            "name: src\nconnection: db\ntable: src\n"
            "grain: [id, snapshot_date]\n"
            "columns:\n"
            "  - {name: id, type: string, nullable: false}\n"
            "  - {name: snapshot_date, type: date, nullable: false}\n"
            "  - {name: val, type: decimal, nullable: true}\n"
            "measures:\n"
            "  - {name: total, expr: 'avg(val)', additivity: non_additive}\n"
            "dimensions:\n"
            "  - {name: snapshot_date, column: snapshot_date}\n"
        )
        (root / "contracts" / "metrics" / "m.yaml").write_text(
            "metric: snap_m\ncanonical:\n"
            "  kind: semi_additive\n"
            "  source: src\n"
            "  measure: total\n"
            "  collapse_dimension: snapshot_date\n"
            "  collapse_agg: last\n"
            "status: active\n"
        )

        with pytest.raises(ContractError, match="additive"):
            validate_contracts(root)


def test_validate_rejects_missing_collapse_dimension() -> None:
    """validate_contracts rejects when collapse_dimension is not a declared dimension."""
    import tempfile
    from pathlib import Path

    from canonic.contracts.validate import validate_contracts
    from canonic.exc import ContractError

    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        (root / "contracts" / "metrics").mkdir(parents=True)
        (root / "semantics" / "db").mkdir(parents=True)

        (root / "semantics" / "db" / "src.yaml").write_text(
            "name: src\nconnection: db\ntable: src\n"
            "grain: [id]\n"
            "columns:\n"
            "  - {name: id, type: string, nullable: false}\n"
            "  - {name: val, type: decimal, nullable: true}\n"
            "measures:\n"
            "  - {name: total, expr: 'sum(val)', additivity: additive}\n"
            "dimensions: []\n"
        )
        (root / "contracts" / "metrics" / "m.yaml").write_text(
            "metric: snap_m\ncanonical:\n"
            "  kind: semi_additive\n"
            "  source: src\n"
            "  measure: total\n"
            "  collapse_dimension: nonexistent_date\n"
            "  collapse_agg: last\n"
            "status: active\n"
        )

        with pytest.raises(ContractError, match="collapse_dimension"):
            validate_contracts(root)
