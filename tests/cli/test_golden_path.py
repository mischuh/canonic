"""Tests for the golden path first-answer steps in ``canon setup`` (OB-S1)."""

from __future__ import annotations

from unittest.mock import AsyncMock

from canon.cli.commands.setup import (
    ReviewTier,
    _best_dimension,
    _classify_withheld,
    _pick_demo_target,
    _render_curated_review,
    _render_describe_fallback,
    _render_setup_complete,
    _render_source_listing,
    _run_golden_path,
    _surface_demo_error,
    _write_bootstrap_contracts,
)
from canon.ingestion.emitter import DiffFormat, EmissionResult, EmittedDiff
from canon.ingestion.models import (
    DraftedBy,
    Proposal,
    ProposalOp,
    ReconciliationDecision,
    ReconciliationEntry,
    ReconciliationReport,
)
from canon.ingestion.pipeline import PipelineResult
from canon.semantic.models import (
    Column,
    Dimension,
    Measure,
    NormalizedType,
    Provenance,
    SemanticSource,
)

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
    assert "demo query failed" in out
    assert "revenue" in out  # describe fallback shows metric names
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


# ---------------------------------------------------------------------------
# Helpers for curated review tests (OB-S4)
# ---------------------------------------------------------------------------


def _withheld_diff(
    target: str = "semantics/conn/orders.yaml",
    drafted_by: DraftedBy = DraftedBy.LLM,
    confidence: float = 0.3,
    anchored_to: list[str] | None = None,
) -> EmittedDiff:
    return EmittedDiff(
        target=target,
        op=ProposalOp.ADD,
        format=DiffFormat.YAML,
        before=None,
        after="",
        patch="",
        provenance=Provenance.INFERRED,
        confidence=confidence,
        drafted_by=drafted_by,
        anchored_to=anchored_to or [],
    )


def _entry_with_content(target: str, content: dict) -> ReconciliationEntry:
    proposal = Proposal(
        target=target,
        op=ProposalOp.ADD,
        content=content,
        provenance=Provenance.INFERRED,
        confidence=0.3,
    )
    return ReconciliationEntry(
        decision=ReconciliationDecision.ADD,
        target=target,
        proposal=proposal,
    )


def _pipeline_result(
    diffs: list[EmittedDiff],
    entries: list[ReconciliationEntry] | None = None,
) -> PipelineResult:
    report = ReconciliationReport(entries=entries or [])
    emission = EmissionResult(diffs=diffs, report=report)
    return PipelineResult(emission=emission, first_run=True)


# ---------------------------------------------------------------------------
# _classify_withheld — ordering (AC1)
# ---------------------------------------------------------------------------


def test_classify_withheld_grain_before_measure_before_long_tail():
    grain = _withheld_diff("semantics/c/a.yaml", drafted_by=DraftedBy.LLM)
    measure = _withheld_diff("semantics/c/b.yaml", drafted_by=DraftedBy.LLM)
    long_tail = _withheld_diff(
        "semantics/c/c.yaml", drafted_by=DraftedBy.DETERMINISTIC, confidence=0.5
    )
    contents = {
        "semantics/c/a.yaml": {"meta": {"grain_draft": True}, "joins": []},
        "semantics/c/b.yaml": {"meta": {}, "joins": []},
        "semantics/c/c.yaml": {"meta": {}, "joins": []},
    }
    items = _classify_withheld([measure, long_tail, grain], contents, {})
    assert [i.tier for i in items] == [ReviewTier.GRAIN, ReviewTier.MEASURE, ReviewTier.LONG_TAIL]


def test_classify_withheld_measure_blast_radius_sorts_by_ref_count():
    a = _withheld_diff("semantics/c/a_low.yaml", drafted_by=DraftedBy.LLM)
    b = _withheld_diff("semantics/c/b_high.yaml", drafted_by=DraftedBy.LLM)
    contents = {
        "semantics/c/a_low.yaml": {"meta": {}, "joins": []},
        "semantics/c/b_high.yaml": {"meta": {}, "joins": []},
    }
    ref_counts = {"a_low": 1, "b_high": 3}
    items = _classify_withheld([a, b], contents, ref_counts)
    assert items[0].source_name == "b_high"
    assert items[1].source_name == "a_low"


def test_classify_withheld_alpha_tiebreak_within_same_tier():
    b = _withheld_diff("semantics/c/b.yaml", drafted_by=DraftedBy.LLM)
    a = _withheld_diff("semantics/c/a.yaml", drafted_by=DraftedBy.LLM)
    contents = {
        "semantics/c/a.yaml": {"meta": {}, "joins": []},
        "semantics/c/b.yaml": {"meta": {}, "joins": []},
    }
    items = _classify_withheld([b, a], contents, {})
    assert items[0].source_name == "a"
    assert items[1].source_name == "b"


# ---------------------------------------------------------------------------
# _render_curated_review — cap + count (AC2) and teachable unit (spec §5)
# ---------------------------------------------------------------------------


def test_render_curated_review_none_returns_zero():
    assert _render_curated_review(None) == 0


def test_render_curated_review_no_withheld_returns_zero(capsys):
    auto_diff = EmittedDiff(
        target="semantics/c/good.yaml",
        op=ProposalOp.ADD,
        format=DiffFormat.YAML,
        before=None,
        after="",
        patch="",
        provenance=Provenance.INFERRED,
        confidence=1.0,
        drafted_by=DraftedBy.DETERMINISTIC,
    )
    result = _pipeline_result([auto_diff])
    count = _render_curated_review(result)
    assert count == 0
    assert capsys.readouterr().out == ""


def test_render_curated_review_caps_and_shows_deferred_count(capsys):
    from canon.cli.commands.setup import _REVIEW_CAP

    diffs = [
        _withheld_diff(f"semantics/c/t{i}.yaml", drafted_by=DraftedBy.LLM)
        for i in range(_REVIEW_CAP + 3)
    ]
    entries = [
        _entry_with_content(f"semantics/c/t{i}.yaml", {"meta": {"grain_draft": True}, "joins": []})
        for i in range(_REVIEW_CAP + 3)
    ]
    result = _pipeline_result(diffs, entries)
    total = _render_curated_review(result)
    out = capsys.readouterr().out

    assert total == _REVIEW_CAP + 3
    assert "… and 3 more" in out
    assert "canon ingest" in out


def test_render_curated_review_grain_teachable_unit(capsys):
    diff = _withheld_diff(
        "semantics/c/orders.yaml",
        drafted_by=DraftedBy.LLM,
        confidence=0.3,
        anchored_to=["sha256:abc123"],
    )
    entry = _entry_with_content(
        "semantics/c/orders.yaml", {"meta": {"grain_draft": True}, "joins": []}
    )
    result = _pipeline_result([diff], [entry])
    _render_curated_review(result)
    out = capsys.readouterr().out

    assert "orders" in out
    assert "0.3" in out
    assert "sha256:abc123" in out
    assert "grain" in out.lower()


def test_render_curated_review_no_anchor_shows_dash(capsys):
    diff = _withheld_diff("semantics/c/orders.yaml", drafted_by=DraftedBy.LLM, anchored_to=[])
    entry = _entry_with_content(
        "semantics/c/orders.yaml", {"meta": {"grain_draft": True}, "joins": []}
    )
    result = _pipeline_result([diff], [entry])
    _render_curated_review(result)
    out = capsys.readouterr().out
    assert "evidence=—" in out


def test_render_curated_review_long_tail_why_line(capsys):
    diff = _withheld_diff(
        "semantics/c/orders.yaml", drafted_by=DraftedBy.DETERMINISTIC, confidence=0.5
    )
    entry = _entry_with_content("semantics/c/orders.yaml", {"meta": {}, "joins": []})
    result = _pipeline_result([diff], [entry])
    _render_curated_review(result)
    out = capsys.readouterr().out
    assert "low-confidence" in out


# ---------------------------------------------------------------------------
# OB-S5 — Honest failure modes
# ---------------------------------------------------------------------------


def _config_no_llm():
    from canon.config import CanonConfig

    return CanonConfig.model_validate(
        {
            "version": 1,
            "project": {"name": "demo"},
            "connections": [],
            "llm": {"provider": "openai_compatible", "base_url": "http://x/v1", "model": ""},
        }
    )


def _config_with_llm():
    from canon.config import CanonConfig

    return CanonConfig.model_validate(
        {
            "version": 1,
            "project": {"name": "demo"},
            "connections": [],
            "llm": {"provider": "openai_compatible", "base_url": "http://x/v1", "model": "m"},
        }
    )


def test_ob_s5_ac1_empty_schema_says_so_no_fabricated_demo(tmp_path, monkeypatch, capsys):
    """AC1: empty/queryless schema → says so plainly, does not fabricate a demo."""
    import canon.cli.commands.setup as setup_mod
    from canon.ingestion.emitter import EmissionResult
    from canon.ingestion.models import ReconciliationReport
    from canon.ingestion.pipeline import PipelineResult

    empty_result = PipelineResult(
        emission=EmissionResult(diffs=[], report=ReconciliationReport(entries=[])),
        first_run=True,
    )
    monkeypatch.setattr(setup_mod, "_bootstrap_connection", lambda *_: empty_result)
    monkeypatch.setattr(setup_mod, "list_semantic_sources", lambda _: [])

    _run_golden_path(tmp_path, _config_with_llm(), [])
    out = capsys.readouterr().out

    assert "no queryable tables found" in out
    assert "step 6 — running first answer" not in out  # demo path never entered


def test_ob_s5_ac2_canon_error_surfaces_registry_code(capsys):
    """AC2: a CanonError from the demo query exposes its registry code — never swallowed."""
    from canon.exc import ConnectionError as CanonConnectionError

    exc = CanonConnectionError("permission denied")
    _surface_demo_error(exc)
    out = capsys.readouterr().out

    assert "connection_error" in out
    assert "permission denied" in out


def test_ob_s5_ac2_demo_canon_error_falls_back_to_describe(tmp_path, monkeypatch, capsys):
    """AC2: CanonError during demo → code surfaced, describe-level fallback rendered, setup completes."""
    import canon.cli.commands.setup as setup_mod
    from canon.exc import ConnectionError as CanonConnectionError

    source = _source(measures=[_additive()], dimensions=[_dim("d", "created_at")])
    monkeypatch.setattr(setup_mod, "_bootstrap_connection", lambda *_: None)
    monkeypatch.setattr(setup_mod, "list_semantic_sources", lambda _: [source])
    monkeypatch.setattr(
        setup_mod,
        "_run_demo_query",
        AsyncMock(side_effect=CanonConnectionError("permission denied")),
    )

    _run_golden_path(tmp_path, _config_with_llm(), [])
    out = capsys.readouterr().out

    assert "connection_error" in out  # registry code surfaced
    assert "permission denied" in out  # message surfaced
    assert "metric" in out.lower()  # describe-level fallback rendered
    assert "setup complete" in out or "what's next" in out  # wizard completes


def test_ob_s5_ac2_describe_fallback_shows_metric_shape(tmp_path, capsys):
    """AC2 describe-level ending: metric list and grain/dimensions shown as shape of a question."""
    source = _source(
        name="sales",
        measures=[_additive("revenue"), _additive("row_count", "id")],
        dimensions=[_dim("order_date", "created_at"), _dim("status", "status")],
    )
    config = _config_with_llm()
    _render_describe_fallback(config, [source])
    out = capsys.readouterr().out

    assert "revenue" in out
    assert "row_count" in out
    assert "what you can ask" in out


def test_ob_s5_ac2_describe_fallback_no_compilable_measures_falls_back_to_listing(capsys):
    """When no measures are p0-compilable, describe fallback still shows source listing."""
    source = _source(name="ledger", measures=[_non_additive()])
    config = _config_with_llm()
    _render_describe_fallback(config, [source])
    out = capsys.readouterr().out

    assert "ledger" in out  # source listing rendered


def test_render_setup_complete_no_llm_model_shows_enrichment_note(capsys, tmp_path):
    """§7 no-LLM honesty: note that naming/prose enrichment needs a model."""
    _render_setup_complete(_config_no_llm(), [], demo_ok=True)
    out = capsys.readouterr().out
    assert "naming/prose enrichment" in out


def test_render_setup_complete_with_llm_no_enrichment_note(capsys, tmp_path):
    """With a model configured the enrichment note is suppressed."""
    _render_setup_complete(_config_with_llm(), [], demo_ok=True)
    out = capsys.readouterr().out
    assert "naming/prose enrichment" not in out
