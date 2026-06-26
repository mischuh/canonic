"""Tests for the golden path first-answer steps in ``canon setup`` (OB-S1)."""

from __future__ import annotations

from unittest.mock import AsyncMock

from canon.cli.commands.setup import (
    _best_dimension,
    _pick_demo_target,
    _render_setup_complete,
    _render_source_listing,
    _run_golden_path,
    _write_bootstrap_contracts,
)
from canon.semantic.models import Column, Dimension, Measure, NormalizedType, SemanticSource

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _source(
    name: str = "orders",
    measures: list[Measure] | None = None,
    dimensions: list[Dimension] | None = None,
    columns: list[Column] | None = None,
) -> SemanticSource:
    cols = columns or [
        Column(name="id", type=NormalizedType.INT, nullable=False),
        Column(name="amount", type=NormalizedType.FLOAT, nullable=False),
        Column(name="created_at", type=NormalizedType.DATE, nullable=False),
        Column(name="status", type=NormalizedType.STRING, nullable=False),
    ]
    return SemanticSource(
        name=name,
        connection="wh",
        table=name,
        grain=["id"],
        columns=cols,
        measures=measures or [],
        dimensions=dimensions or [],
    )


def _additive(name: str = "revenue", col: str = "amount") -> Measure:
    return Measure(name=name, expr=f"sum({col})", additivity="additive")


def _non_additive(name: str = "balance") -> Measure:
    return Measure(name=name, expr="last_value(amount)", additivity="non_additive")


def _dim(name: str, column: str) -> Dimension:
    return Dimension(name=name, column=column)


# ---------------------------------------------------------------------------
# _pick_demo_target
# ---------------------------------------------------------------------------


def test_pick_demo_target_empty():
    assert _pick_demo_target([]) == (None, None, None)


def test_pick_demo_target_no_compilable_measures():
    source = _source(measures=[_non_additive()])
    assert _pick_demo_target([source]) == (None, None, None)


def test_pick_demo_target_single_source_no_dim():
    m = _additive()
    source = _source(measures=[m])
    s, measure, dim = _pick_demo_target([source])
    assert s is source
    assert measure is m
    assert dim is None


def test_pick_demo_target_single_source_with_dim():
    m = _additive()
    d = _dim("order_date", "created_at")
    source = _source(measures=[m], dimensions=[d])
    s, measure, dim = _pick_demo_target([source])
    assert s is source
    assert measure is m
    assert dim is d


def test_pick_demo_target_prefers_most_measures():
    rich = _source(
        name="b_rich",
        measures=[_additive("rev"), _additive("cnt", "id")],
        columns=[
            Column(name="id", type=NormalizedType.INT, nullable=False),
            Column(name="amount", type=NormalizedType.FLOAT, nullable=False),
        ],
    )
    sparse = _source(name="a_sparse", measures=[_additive()])
    s, _, _ = _pick_demo_target([sparse, rich])
    assert s is rich


def test_pick_demo_target_name_tiebreak():
    m = _additive()
    a = _source(name="aaa", measures=[m])
    b = _source(name="bbb", measures=[m])
    s, _, _ = _pick_demo_target([b, a])
    assert s is a


# ---------------------------------------------------------------------------
# _best_dimension
# ---------------------------------------------------------------------------


def test_best_dimension_no_dimensions():
    assert _best_dimension(_source()) is None


def test_best_dimension_prefers_date():
    date_dim = _dim("day", "created_at")
    text_dim = _dim("status", "status")
    source = _source(dimensions=[text_dim, date_dim])
    assert _best_dimension(source) is date_dim


def test_best_dimension_prefers_boolean():
    cols = [
        Column(name="id", type=NormalizedType.INT, nullable=False),
        Column(name="is_active", type=NormalizedType.BOOL, nullable=False),
        Column(name="label", type=NormalizedType.STRING, nullable=False),
    ]
    bool_dim = _dim("active", "is_active")
    text_dim = _dim("label", "label")
    source = _source(columns=cols, dimensions=[text_dim, bool_dim])
    assert _best_dimension(source) is bool_dim


def test_best_dimension_falls_back_to_first():
    source = _source(dimensions=[_dim("status", "status"), _dim("label", "status")])
    result = _best_dimension(source)
    assert result is source.dimensions[0]


# ---------------------------------------------------------------------------
# _render_* smoke tests — verify no crash and key strings present
# ---------------------------------------------------------------------------


def test_render_source_listing_smoke(capsys):
    sources = [_source(name="orders", measures=[_additive()], dimensions=[_dim("d", "created_at")])]
    _render_source_listing(sources)
    out = capsys.readouterr().out
    assert "orders" in out
    assert "1 measure" in out


def test_render_source_listing_truncates_at_five(capsys):
    sources = [_source(name=f"t{i}", measures=[_additive()]) for i in range(7)]
    _render_source_listing(sources)
    out = capsys.readouterr().out
    assert "and 2 more" in out


def test_render_setup_complete_smoke(capsys, tmp_path):
    from canon.config import CanonConfig

    config = CanonConfig.model_validate(
        {
            "version": 1,
            "project": {"name": "my-project"},
            "connections": [],
            "llm": {"provider": "openai_compatible", "base_url": "http://x/v1", "model": "m"},
        }
    )
    _render_setup_complete(config, [tmp_path / "semantics"], demo_ok=True)
    out = capsys.readouterr().out
    assert "my-project" in out
    assert "canon ingest" in out
    assert "canon query" in out
    assert "canon mcp start" in out


def test_render_setup_complete_no_demo_shows_tip(capsys, tmp_path):
    from canon.config import CanonConfig

    config = CanonConfig.model_validate(
        {
            "version": 1,
            "project": {"name": "p"},
            "connections": [],
            "llm": {"provider": "openai_compatible", "base_url": "http://x/v1", "model": "m"},
        }
    )
    _render_setup_complete(config, [], demo_ok=False)
    out = capsys.readouterr().out
    assert "tip:" in out


# ---------------------------------------------------------------------------
# _run_golden_path integration — bootstrap fail gracefully
# ---------------------------------------------------------------------------


def test_run_golden_path_bootstrap_failure_still_completes(tmp_path, capsys):
    """Setup completes even when bootstrap raises (no connection available)."""
    from canon.config import CanonConfig

    config = CanonConfig.model_validate(
        {
            "version": 1,
            "project": {"name": "demo", "default_connection": "wh"},
            "connections": [
                {
                    "id": "wh",
                    "type": "postgres",
                    "params": {"host": "localhost", "port": 5432, "dbname": "db", "user": "u"},
                    "credentials_ref": "env:MISSING_VAR",
                }
            ],
            "llm": {"provider": "openai_compatible", "base_url": "http://x/v1", "model": "m"},
        }
    )
    # MISSING_VAR is not set → CredentialError → graceful skip
    _run_golden_path(tmp_path, config, [])
    out = capsys.readouterr().out
    assert "setup complete" in out or "what's next" in out


def test_run_golden_path_no_sources_after_bootstrap(tmp_path, capsys):
    """When bootstrap finds nothing queryable, the completion panel still shows."""
    from canon.config import CanonConfig

    config = CanonConfig.model_validate(
        {
            "version": 1,
            "project": {"name": "demo"},
            "connections": [],
            "llm": {"provider": "openai_compatible", "base_url": "http://x/v1", "model": "m"},
        }
    )
    _run_golden_path(tmp_path, config, [])
    out = capsys.readouterr().out
    assert "demo" in out


def test_run_golden_path_sources_no_compilable_measure(tmp_path, monkeypatch, capsys):
    """Sources with no p0-compilable measures fall back to the source listing."""
    import canon.cli.commands.setup as setup_mod
    from canon.config import CanonConfig

    config = CanonConfig.model_validate(
        {
            "version": 1,
            "project": {"name": "demo"},
            "connections": [],
            "llm": {"provider": "openai_compatible", "base_url": "http://x/v1", "model": "m"},
        }
    )
    source = _source(measures=[_non_additive()])
    monkeypatch.setattr(setup_mod, "_bootstrap_connection", lambda *_: None)
    monkeypatch.setattr(setup_mod, "list_semantic_sources", lambda _: [source])

    _run_golden_path(tmp_path, config, [])
    out = capsys.readouterr().out
    assert "orders" in out  # source listing shown


def test_run_golden_path_demo_query_error_falls_back(tmp_path, monkeypatch, capsys):
    """A failing demo query shows the source listing and still completes setup."""
    import canon.cli.commands.setup as setup_mod
    from canon.config import CanonConfig

    config = CanonConfig.model_validate(
        {
            "version": 1,
            "project": {"name": "demo"},
            "connections": [],
            "llm": {"provider": "openai_compatible", "base_url": "http://x/v1", "model": "m"},
        }
    )
    source = _source(measures=[_additive()], dimensions=[_dim("d", "created_at")])
    monkeypatch.setattr(setup_mod, "_bootstrap_connection", lambda *_: None)
    monkeypatch.setattr(setup_mod, "list_semantic_sources", lambda _: [source])
    monkeypatch.setattr(
        setup_mod, "_run_demo_query", AsyncMock(side_effect=RuntimeError("db down"))
    )

    _run_golden_path(tmp_path, config, [])
    out = capsys.readouterr().out
    assert "demo query skipped" in out
    assert "orders" in out  # fallback listing shown
    assert "demo" in out  # completion panel shown


# ---------------------------------------------------------------------------
# _write_bootstrap_contracts
# ---------------------------------------------------------------------------


def test_write_bootstrap_contracts_creates_files(tmp_path):
    source = _source(measures=[_additive("revenue"), _additive("order_count", "id")])
    count = _write_bootstrap_contracts(tmp_path, [source])
    assert count == 2
    revenue_file = tmp_path / "contracts" / "metrics" / "revenue.yaml"
    assert revenue_file.exists()
    text = revenue_file.read_text()
    assert "metric: revenue" in text
    assert "source: orders" in text
    assert "measure: revenue" in text
    assert "provenance: inferred" in text
    assert "status: active" in text


def test_write_bootstrap_contracts_skips_non_compilable(tmp_path):
    source = _source(measures=[_additive("revenue"), _non_additive("balance")])
    count = _write_bootstrap_contracts(tmp_path, [source])
    assert count == 1
    assert (tmp_path / "contracts" / "metrics" / "revenue.yaml").exists()
    assert not (tmp_path / "contracts" / "metrics" / "balance.yaml").exists()


def test_write_bootstrap_contracts_idempotent(tmp_path):
    source = _source(measures=[_additive("revenue")])
    _write_bootstrap_contracts(tmp_path, [source])
    (tmp_path / "contracts" / "metrics" / "revenue.yaml").write_text("custom: content\n")
    count = _write_bootstrap_contracts(tmp_path, [source])
    assert count == 0  # file already exists → not overwritten
    assert "custom: content" in (tmp_path / "contracts" / "metrics" / "revenue.yaml").read_text()


def test_write_bootstrap_contracts_slug_uses_hyphens(tmp_path):
    source = _source(measures=[_additive("total_order_revenue", "amount")])
    _write_bootstrap_contracts(tmp_path, [source])
    assert (tmp_path / "contracts" / "metrics" / "total-order-revenue.yaml").exists()


def test_write_bootstrap_contracts_empty_sources(tmp_path):
    assert _write_bootstrap_contracts(tmp_path, []) == 0


def test_write_bootstrap_contracts_falls_back_to_columns_when_no_measures(tmp_path):
    """Source with measures=[] but numeric columns → inferred contracts via column fallback."""
    source = _source(
        measures=[],
        columns=[
            Column(name="id", type=NormalizedType.INT, nullable=False),
            Column(name="amount", type=NormalizedType.FLOAT, nullable=True),
            Column(name="status", type=NormalizedType.STRING, nullable=True),
        ],
    )
    count = _write_bootstrap_contracts(tmp_path, [source])
    assert count == 2  # row_count + total_amount (id skipped, status is a dimension type)
    assert (tmp_path / "contracts" / "metrics" / "row-count.yaml").exists()
    assert (tmp_path / "contracts" / "metrics" / "total-amount.yaml").exists()
    text = (tmp_path / "contracts" / "metrics" / "total-amount.yaml").read_text()
    assert "metric: total_amount" in text
    assert "source: orders" in text


def test_write_bootstrap_contracts_fallback_skips_id_columns(tmp_path):
    """id and *_id columns are excluded from fallback sum measures."""
    source = _source(
        measures=[],
        columns=[
            Column(name="id", type=NormalizedType.INT, nullable=False),
            Column(name="customer_id", type=NormalizedType.INT, nullable=False),
            Column(name="revenue", type=NormalizedType.DECIMAL, nullable=True),
        ],
    )
    count = _write_bootstrap_contracts(tmp_path, [source])
    assert count == 2  # row_count + total_revenue
    names = {p.stem for p in (tmp_path / "contracts" / "metrics").iterdir()}
    assert "row-count" in names
    assert "total-revenue" in names
    assert "total-id" not in names
    assert "total-customer-id" not in names
