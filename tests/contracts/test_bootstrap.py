"""Tests for canonic/contracts/bootstrap.py."""

from __future__ import annotations

from canonic.contracts.bootstrap import infer_p0_pairs, write_inferred_contracts
from canonic.semantic.models import Column, Measure, NormalizedType, SemanticSource


def _source(
    name: str = "orders",
    measures: list[Measure] | None = None,
    columns: list[Column] | None = None,
) -> SemanticSource:
    cols = columns or [
        Column(name="id", type=NormalizedType.INT, nullable=False),
        Column(name="amount", type=NormalizedType.FLOAT, nullable=True),
    ]
    return SemanticSource(
        name=name,
        connection="wh",
        table=name,
        grain=["id"],
        columns=cols,
        measures=measures or [],
        dimensions=[],
    )


def _additive(name: str = "revenue", col: str = "amount") -> Measure:
    return Measure(name=name, expr=f"sum({col})", additivity="additive")


# ---------------------------------------------------------------------------
# infer_p0_pairs
# ---------------------------------------------------------------------------


def test_infer_p0_pairs_always_has_row_count():
    source = _source(columns=[])
    pairs = infer_p0_pairs(source)
    assert ("row_count", "count(*)") in pairs


def test_infer_p0_pairs_sum_for_numeric_non_id():
    source = _source(
        columns=[
            Column(name="id", type=NormalizedType.INT, nullable=False),  # grain col
            Column(name="amount", type=NormalizedType.FLOAT, nullable=True),
            Column(name="qty", type=NormalizedType.INT, nullable=False),
        ]
    )
    names = {n for n, _ in infer_p0_pairs(source)}
    assert "total_amount" in names
    assert "total_qty" in names
    assert "total_id" not in names  # id is excluded


def test_infer_p0_pairs_skips_id_columns():
    source = _source(
        columns=[
            Column(name="id", type=NormalizedType.INT, nullable=False),
            Column(name="customer_id", type=NormalizedType.INT, nullable=False),
            Column(name="ref_fk", type=NormalizedType.INT, nullable=False),
        ]
    )
    names = {n for n, _ in infer_p0_pairs(source)}
    assert names == {"row_count"}


# ---------------------------------------------------------------------------
# write_inferred_contracts
# ---------------------------------------------------------------------------


def test_write_inferred_contracts_creates_files(tmp_path):
    source = _source(measures=[_additive("revenue"), _additive("order_count", "id")])
    count = write_inferred_contracts(tmp_path, [source])
    assert count == 2
    text = (tmp_path / "contracts" / "metrics" / "revenue.yaml").read_text()
    assert "metric: revenue" in text
    assert "source: orders" in text
    assert "provenance: human_curated" in text
    assert "status: active" in text


def test_write_inferred_contracts_fallback_from_columns(tmp_path):
    """Empty measures → column-type inference kicks in."""
    source = _source(
        measures=[],
        columns=[
            Column(name="id", type=NormalizedType.INT, nullable=False),
            Column(name="amount", type=NormalizedType.FLOAT, nullable=True),
        ],
    )
    count = write_inferred_contracts(tmp_path, [source])
    assert count == 2
    assert (tmp_path / "contracts" / "metrics" / "row-count.yaml").exists()
    assert (tmp_path / "contracts" / "metrics" / "total-amount.yaml").exists()


def test_write_inferred_contracts_idempotent(tmp_path):
    source = _source(measures=[_additive("revenue")])
    write_inferred_contracts(tmp_path, [source])
    (tmp_path / "contracts" / "metrics" / "revenue.yaml").write_text("custom: content\n")
    count = write_inferred_contracts(tmp_path, [source])
    assert count == 0
    assert "custom: content" in (tmp_path / "contracts" / "metrics" / "revenue.yaml").read_text()


def test_write_inferred_contracts_empty_sources(tmp_path):
    assert write_inferred_contracts(tmp_path, []) == 0


def test_write_inferred_contracts_qualifies_cross_source_collision(tmp_path, caplog):
    """Two sources sharing a measure name both get contracts, not a silent drop."""
    customers = _source(name="customers", measures=[_additive("row_count", "id")])
    products = _source(name="products", measures=[_additive("row_count", "id")])

    with caplog.at_level("WARNING"):
        count = write_inferred_contracts(tmp_path, [customers, products])

    assert count == 2
    plain = tmp_path / "contracts" / "metrics" / "row-count.yaml"
    qualified = tmp_path / "contracts" / "metrics" / "products-row-count.yaml"
    assert plain.exists()
    assert qualified.exists()
    assert "metric: row_count" in plain.read_text()
    assert "source: customers" in plain.read_text()
    assert "metric: products_row_count" in qualified.read_text()
    assert "source: products" in qualified.read_text()
    assert "measure: row_count" in qualified.read_text()  # canonical.measure stays unqualified
    assert any("collides with an existing contract" in msg for msg in caplog.messages)


def test_write_inferred_contracts_same_source_rerun_is_idempotent_not_qualified(tmp_path):
    """Re-running for the *same* source is a no-op, not a qualified duplicate."""
    source = _source(name="orders", measures=[_additive("revenue")])
    write_inferred_contracts(tmp_path, [source])
    count = write_inferred_contracts(tmp_path, [source])
    assert count == 0
    assert not (tmp_path / "contracts" / "metrics" / "orders-revenue.yaml").exists()


def test_write_inferred_contracts_slug_uses_hyphens(tmp_path):
    source = _source(measures=[_additive("total_order_revenue", "amount")])
    write_inferred_contracts(tmp_path, [source])
    assert (tmp_path / "contracts" / "metrics" / "total-order-revenue.yaml").exists()
