"""Tests for canon/ingestion/pipeline.py (GH-37) — SPEC-E4 §2 orchestration, §7, §8.

Drives the full four-stage pipeline through a fake connector (no live DB) so the proposal and
file shapes are exactly production. Covers idempotency (S6), determinism (S9-AC1), the
validation gate (S8), the bootstrap write path (§8), dry-run write suppression, and the
E10 mode switch (GH-68): headless pins NullLLMDrafter; interactive with LLM uses
RuntimeLLMDrafter; no-models configured uses NullLLMDrafter deterministically.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

from canon.config import (
    LOCAL_STATE_DIR,
    LLMConfig,
    ReconcileConfig,
    RuntimeConfig,
    scaffold_project,
)
from canon.connectors.base import (
    Capability,
    ColumnInfo,
    ConnectorBase,
    ForeignKey,
    ForeignKeyRef,
    Health,
    RelationSchema,
    compute_fingerprint,
)
from canon.exc import ValidationFailed
from canon.ingestion.models import DraftedBy, ProposalOp, ReconciliationDecision
from canon.ingestion.pipeline import IngestionPipeline, first_run_auto_acceptable
from canon.ingestion.source import evidence_from_introspection
from canon.runtime.drafter import make_drafter
from canon.semantic.loader import load_semantic_source

if TYPE_CHECKING:
    from pathlib import Path

_CONN = "warehouse_pg"


class FakeConnector(ConnectorBase):
    """A connector whose ``introspect_schema`` returns canned relations (tier-1 live)."""

    def __init__(self, schemas: list[RelationSchema]) -> None:
        self._schemas = schemas

    def capabilities(self) -> list[Capability]:
        return [Capability.INTROSPECT_SCHEMA, Capability.TEST_CONNECTION]

    async def test_connection(self) -> Health:
        return Health(status="ok")

    async def introspect_schema(self) -> list[RelationSchema]:
        return list(self._schemas)


def _schema(
    relation: str,
    columns: list[ColumnInfo],
    primary_key: list[str],
    foreign_keys: list[ForeignKey] | None = None,
) -> RelationSchema:
    fks = foreign_keys or []
    return RelationSchema(
        connection=_CONN,
        relation=relation,
        kind="table",
        columns=columns,
        primary_key=primary_key,
        foreign_keys=fks,
        acquisition_tier="live",
        source_fingerprint=compute_fingerprint(columns, primary_key, fks),
    )


def _customers() -> RelationSchema:
    return _schema(
        "analytics.dim_customers",
        [
            ColumnInfo(name="customer_id", type="int", nullable=False),
            ColumnInfo(name="name", type="string"),
        ],
        ["customer_id"],
    )


def _orders(*, extra: bool = False) -> RelationSchema:
    columns = [
        ColumnInfo(name="order_id", type="int", nullable=False),
        ColumnInfo(name="customer_id", type="int", nullable=False),
        ColumnInfo(name="amount", type="decimal"),
    ]
    if extra:  # a new column shifts the fingerprint → drift (S6-AC2)
        columns.append(ColumnInfo(name="discount", type="decimal"))
    fk = ForeignKey(
        columns=["customer_id"],
        references=ForeignKeyRef(relation="analytics.dim_customers", columns=["customer_id"]),
    )
    return _schema("analytics.fct_orders", columns, ["order_id"], [fk])


def _pipeline(root: Path, schemas: list[RelationSchema]) -> IngestionPipeline:
    scaffold_project(root)
    return IngestionPipeline(root, {_CONN: FakeConnector(schemas)}, ReconcileConfig())


def _pipeline_no_scaffold(root: Path, schemas: list[RelationSchema]) -> IngestionPipeline:
    """Like ``_pipeline`` but skips scaffold_project — for re-runs on an already-set-up root."""
    return IngestionPipeline(root, {_CONN: FakeConnector(schemas)}, ReconcileConfig())


# ---------------------------------------------------------------------------
# §8 — fast initial bootstrap
# ---------------------------------------------------------------------------


async def test_bootstrap_writes_a_semantic_file_per_table(tmp_path: Path) -> None:
    """`bootstrap` introspects the connection and writes semantics/<conn>/<name>.yaml (§8)."""
    pipeline = _pipeline(tmp_path, [_customers(), _orders()])

    result = await pipeline.bootstrap(_CONN)

    assert (tmp_path / "semantics" / _CONN / "dim_customers.yaml").exists()
    assert (tmp_path / "semantics" / _CONN / "fct_orders.yaml").exists()
    assert {d.op for d in result.emission.diffs} == {ProposalOp.ADD}
    # Every written diff is deterministic — the core (PK tables) (OB-S2 AC2).
    assert all(d.drafted_by is DraftedBy.DETERMINISTIC for d in result.emission.diffs)
    # The written files round-trip as valid semantic sources.
    orders = load_semantic_source(tmp_path / "semantics" / _CONN / "fct_orders.yaml")
    assert orders.grain == ["order_id"]


async def test_bootstrap_withholds_llm_drafted_proposals(tmp_path: Path) -> None:
    """OB-S2 AC2: a no-PK table is withheld (drafted_by: llm) — its file is not written."""
    pipeline = _pipeline(tmp_path, [_customers(), _no_pk_schema()])

    result = await pipeline.bootstrap(_CONN)

    # PK table is written; no-PK table is withheld.
    assert (tmp_path / "semantics" / _CONN / "dim_customers.yaml").exists()
    assert not (tmp_path / "semantics" / _CONN / "events.yaml").exists()

    withheld = [d for d in result.emission.diffs if not first_run_auto_acceptable(d)]
    assert len(withheld) == 1
    assert withheld[0].drafted_by is DraftedBy.LLM


async def test_bootstrap_is_one_time_subsequent_ingest_is_propose_only(tmp_path: Path) -> None:
    """OB-S3 AC2: after bootstrap, a subsequent run() uses propose-only (no auto-write)."""
    pipeline = _pipeline(tmp_path, [_customers(), _no_pk_schema()])
    await pipeline.bootstrap(_CONN)

    evidence = await evidence_from_introspection(
        FakeConnector([_customers(), _no_pk_schema()]), _CONN
    )
    result = await pipeline.run(evidence)

    # The no-PK table was withheld at bootstrap time so it is still an ADD proposal on re-run.
    add_diffs = [d for d in result.emission.diffs if d.op is ProposalOp.ADD]
    assert len(add_diffs) == 1
    assert "events" in add_diffs[0].target
    # propose-only: the file is still absent (not auto-written).
    assert not (tmp_path / "semantics" / _CONN / "events.yaml").exists()


async def test_re_bootstrap_on_non_empty_project_is_propose_only(tmp_path: Path) -> None:
    """OB-S3 AC2: re-running bootstrap() when accepted context exists never auto-writes.

    Covers the ``canon ingest --bootstrap`` re-run hole: a brand-new PK table appearing
    upstream must not be silently written into the project.
    """
    pipeline = _pipeline(tmp_path, [_customers()])
    first = await pipeline.bootstrap(_CONN)

    assert first.first_run is True
    assert (tmp_path / "semantics" / _CONN / "dim_customers.yaml").exists()

    # Simulate a new upstream PK table appearing after initial bootstrap.
    pipeline2 = _pipeline_no_scaffold(tmp_path, [_customers(), _orders()])
    second = await pipeline2.bootstrap(_CONN)

    assert second.first_run is False
    # The new table must NOT have been auto-written — it lands as an ADD proposal only.
    assert not (tmp_path / "semantics" / _CONN / "fct_orders.yaml").exists()
    add_diffs = [d for d in second.emission.diffs if d.op is ProposalOp.ADD]
    assert any("fct_orders" in d.target for d in add_diffs)


async def test_auto_accepted_files_are_visible_in_git_working_tree(tmp_path: Path) -> None:
    """OB-S3 AC1: every auto-accepted file is an untracked working-tree change — revertable."""
    import subprocess

    subprocess.run(["git", "init", str(tmp_path)], check=True, capture_output=True)
    pipeline = _pipeline(tmp_path, [_customers(), _orders()])
    await pipeline.bootstrap(_CONN)

    result = subprocess.run(
        ["git", "status", "--porcelain", "-u"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=True,
    )
    # All written semantics files show up as untracked (??).
    untracked = {line[3:] for line in result.stdout.splitlines() if line.startswith("??")}
    assert any("dim_customers" in p for p in untracked)
    assert any("fct_orders" in p for p in untracked)

    # Revert via git clean — no trace left in committed state.
    subprocess.run(["git", "clean", "-fd"], cwd=tmp_path, check=True, capture_output=True)
    assert not (tmp_path / "semantics" / _CONN / "dim_customers.yaml").exists()
    assert not (tmp_path / "semantics" / _CONN / "fct_orders.yaml").exists()


# ---------------------------------------------------------------------------
# S6 — idempotent re-run
# ---------------------------------------------------------------------------


async def test_unchanged_rerun_proposes_zero_diffs_and_refreshes_last_validated_at(
    tmp_path: Path,
) -> None:
    """No upstream change → zero diffs, only last_validated_at refreshed (S6-AC1)."""
    pipeline = _pipeline(tmp_path, [_customers(), _orders()])
    await pipeline.bootstrap(_CONN)

    evidence = await evidence_from_introspection(FakeConnector([_customers(), _orders()]), _CONN)
    result = await pipeline.run(evidence)

    assert result.emission.diffs == []
    decisions = {e.decision for e in result.report.entries}
    assert decisions == {ReconciliationDecision.NO_OP}
    # The no-op refresh stamped last_validated_at on the unchanged accepted file.
    orders = load_semantic_source(tmp_path / "semantics" / _CONN / "fct_orders.yaml")
    assert orders.meta.last_validated_at is not None


async def test_changed_fingerprint_produces_exactly_the_affected_edit(tmp_path: Path) -> None:
    """A drifted source_fingerprint → exactly one edit for the affected target (S6-AC2)."""
    pipeline = _pipeline(tmp_path, [_customers(), _orders()])
    await pipeline.bootstrap(_CONN)

    evidence = await evidence_from_introspection(
        FakeConnector([_customers(), _orders(extra=True)]), _CONN
    )
    result = await pipeline.run(evidence)

    (diff,) = result.emission.diffs
    assert diff.op is ProposalOp.EDIT
    assert diff.target == f"semantics/{_CONN}/fct_orders.yaml"


# ---------------------------------------------------------------------------
# S9-AC1 — headless determinism
# ---------------------------------------------------------------------------


async def test_two_runs_with_identical_inputs_are_byte_identical(tmp_path: Path) -> None:
    """Identical evidence + accepted state → byte-identical emission (S9-AC1)."""
    pipeline = _pipeline(tmp_path, [_customers(), _orders()])
    evidence = await evidence_from_introspection(FakeConnector([_customers(), _orders()]), _CONN)

    first = await pipeline.run(evidence)
    second = await pipeline.run(evidence)

    assert first.emission.to_json() == second.emission.to_json()


# ---------------------------------------------------------------------------
# S8 — validation gates emission
# ---------------------------------------------------------------------------


async def test_invalid_proposal_raises_before_any_diff(tmp_path: Path) -> None:
    """A proposal whose grain references an undeclared column → ValidationFailed (S8-AC1)."""
    bad = _schema(
        "analytics.broken",
        [ColumnInfo(name="id", type="int", nullable=False)],
        ["ghost"],  # not a declared column
    )
    pipeline = _pipeline(tmp_path, [bad])
    evidence = await evidence_from_introspection(FakeConnector([bad]), _CONN)

    with pytest.raises(ValidationFailed):
        await pipeline.run(evidence)


# ---------------------------------------------------------------------------
# dry-run writes nothing
# ---------------------------------------------------------------------------


async def test_dry_run_touches_no_file(tmp_path: Path) -> None:
    """--dry-run computes diffs but writes neither snapshot, event log, nor semantics."""
    pipeline = _pipeline(tmp_path, [_customers(), _orders()])
    evidence = await evidence_from_introspection(FakeConnector([_customers(), _orders()]), _CONN)

    result = await pipeline.run(evidence, dry_run=True)

    assert {d.op for d in result.emission.diffs} == {ProposalOp.ADD}  # would-be adds
    assert not list((tmp_path / "raw-sources").rglob("*.jsonl"))
    assert not (tmp_path / LOCAL_STATE_DIR / "events.jsonl").exists()
    assert not (tmp_path / "semantics" / _CONN).exists()


# ---------------------------------------------------------------------------
# GH-68 — E10 mode switch: headless off, interactive on, no-models deterministic
# ---------------------------------------------------------------------------


def _no_pk_schema() -> RelationSchema:
    """A relation with no primary key — triggers drafter.draft_grain() when active."""
    return _schema(
        "analytics.events",
        [
            ColumnInfo(name="event_id", type="string", nullable=False),
            ColumnInfo(name="user_id", type="int", nullable=False),
            ColumnInfo(name="ts", type="timestamp", nullable=False),
        ],
        [],  # no primary key → grain is drafted
    )


_LLM_CONFIG = LLMConfig(
    provider="openai_compatible",
    base_url="http://localhost:11434/v1",
    model="small-local",
)


async def test_headless_mode_makes_zero_model_calls(
    tmp_path: Path, fake_litellm: dict[str, Any]
) -> None:
    """Headless + LLM configured → NullLLMDrafter is pinned, zero litellm calls (GH-68 S7)."""
    drafter = make_drafter(_LLM_CONFIG, RuntimeConfig(), headless=True)
    scaffold_project(tmp_path)
    pipeline = IngestionPipeline(
        tmp_path,
        {_CONN: FakeConnector([_no_pk_schema()])},
        ReconcileConfig(),
        headless=True,
        drafter=drafter,
    )
    evidence = await evidence_from_introspection(FakeConnector([_no_pk_schema()]), _CONN)

    await pipeline.run(evidence)

    assert len(fake_litellm["_calls"]) == 0


async def test_headless_mode_is_byte_identical_across_runs(
    tmp_path: Path, fake_litellm: dict[str, Any]
) -> None:
    """Headless with a relation lacking PK → deterministic NullLLMDrafter, byte-identical output."""
    drafter = make_drafter(_LLM_CONFIG, RuntimeConfig(), headless=True)
    scaffold_project(tmp_path)
    pipeline = IngestionPipeline(
        tmp_path,
        {_CONN: FakeConnector([_no_pk_schema()])},
        ReconcileConfig(),
        headless=True,
        drafter=drafter,
    )
    evidence = await evidence_from_introspection(FakeConnector([_no_pk_schema()]), _CONN)

    first = await pipeline.run(evidence)
    second = await pipeline.run(evidence)

    assert first.emission.to_json() == second.emission.to_json()
    assert len(fake_litellm["_calls"]) == 0


async def test_interactive_mode_calls_drafter_for_no_pk_relation(tmp_path: Path) -> None:
    """Interactive mode wires the injected drafter; draft_grain is called for a no-PK relation."""
    from canon.ingestion.builder import GrainDraft

    grain_calls: list[Any] = []

    class SpyDrafter:
        async def draft_grain(self, schema: Any) -> GrainDraft:
            grain_calls.append(schema)
            return GrainDraft(grain=[], confidence=0.5)

        async def draft_dimension_labels(
            self, schema: Any, dimensions: list[dict[str, Any]]
        ) -> list[Any]:
            return []

        async def draft_joins(self, observed: dict[str, Any]) -> list[dict[str, Any]]:
            return []

        async def draft_schema_joins(
            self, schema: Any, candidate_columns: list[str], other_relations: dict[str, Any]
        ) -> list[Any]:
            return []

    scaffold_project(tmp_path)
    pipeline = IngestionPipeline(
        tmp_path,
        {_CONN: FakeConnector([_no_pk_schema()])},
        ReconcileConfig(),
        headless=False,
        drafter=SpyDrafter(),
    )
    evidence = await evidence_from_introspection(FakeConnector([_no_pk_schema()]), _CONN)

    await pipeline.run(evidence)

    assert len(grain_calls) == 1
    assert grain_calls[0].relation == "analytics.events"


async def test_first_run_excludes_relation_with_drafted_join(tmp_path: Path) -> None:
    """A guessed FK-less join always forces drafted_by/confidence down — a wrong join must
    never let a PK-bearing relation slip past first_run_auto_acceptable unreviewed."""
    from canon.ingestion.builder import GrainDraft, JoinDraft

    class StubDrafter:
        async def draft_grain(self, schema: Any) -> GrainDraft:
            return GrainDraft(grain=[], confidence=0.5)

        async def draft_dimension_labels(
            self, schema: Any, dimensions: list[dict[str, Any]]
        ) -> list[Any]:
            return []

        async def draft_joins(self, observed: dict[str, Any]) -> list[dict[str, Any]]:
            return []

        async def draft_schema_joins(
            self, schema: Any, candidate_columns: list[str], other_relations: dict[str, Any]
        ) -> list[JoinDraft]:
            if not candidate_columns:
                return []
            return [
                JoinDraft(
                    column=candidate_columns[0],
                    to=next(iter(other_relations)),
                    to_column=candidate_columns[0],
                    confidence=0.9,
                    reasoning="stub",
                )
            ]

    scaffold_project(tmp_path)
    pipeline = IngestionPipeline(
        tmp_path,
        {_CONN: FakeConnector([_customers(), _orders()])},
        ReconcileConfig(),
        headless=False,
        drafter=StubDrafter(),
    )
    evidence = await evidence_from_introspection(FakeConnector([_customers(), _orders()]), _CONN)

    result = await pipeline.run(evidence)

    customers_diff = next(d for d in result.emission.diffs if "dim_customers" in d.target)
    assert customers_diff.drafted_by is DraftedBy.LLM
    assert customers_diff.confidence < 1.0
    assert not first_run_auto_acceptable(customers_diff)


async def test_no_models_configured_headless_is_deterministic(tmp_path: Path) -> None:
    """llm=None + headless → NullLLMDrafter, zero model calls, deterministic emission (GH-68 S7)."""
    drafter = make_drafter(None, RuntimeConfig(), headless=True)
    scaffold_project(tmp_path)
    pipeline = IngestionPipeline(
        tmp_path,
        {_CONN: FakeConnector([_no_pk_schema()])},
        ReconcileConfig(),
        headless=True,
        drafter=drafter,
    )
    evidence = await evidence_from_introspection(FakeConnector([_no_pk_schema()]), _CONN)

    first = await pipeline.run(evidence)
    second = await pipeline.run(evidence)

    assert first.emission.to_json() == second.emission.to_json()
