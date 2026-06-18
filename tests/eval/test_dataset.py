"""Dataset and candidate loading for the baseline harness (SPEC-E10 §7)."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from pathlib import Path

from canon.eval.candidates import load_candidates
from canon.eval.dataset import default_dataset_path, load_grain_cases
from canon.exc import EvalDatasetError


def test_shipped_dataset_loads_into_schemas() -> None:
    cases = load_grain_cases(default_dataset_path())

    assert len(cases) >= 8  # the shipped labeled set
    junction = next(c for c in cases if c.relation == "app.order_items")
    assert set(junction.expected_grain) == {"order_id", "product_id"}
    # to_schema() builds a real RelationSchema with no declared primary key (what E4 drafts for).
    schema = junction.to_schema()
    assert schema.primary_key == []
    assert {c.name for c in schema.columns} == {"order_id", "product_id", "quantity", "unit_price"}


def test_invalid_json_line_raises_with_line_number(tmp_path: Path) -> None:
    bad = tmp_path / "cases.jsonl"
    bad.write_text('{"relation": "ok", "columns": [], "expected_grain": []}\nnot json\n')

    with pytest.raises(EvalDatasetError, match=r"cases\.jsonl:2: invalid JSON"):
        load_grain_cases(bad)


def test_schema_violation_raises_eval_dataset_error(tmp_path: Path) -> None:
    bad = tmp_path / "cases.jsonl"
    bad.write_text('{"relation": "r", "columns": []}\n')  # missing expected_grain

    with pytest.raises(EvalDatasetError, match="invalid grain case"):
        load_grain_cases(bad)


def test_empty_dataset_raises(tmp_path: Path) -> None:
    empty = tmp_path / "cases.jsonl"
    empty.write_text("# only a comment\n\n")

    with pytest.raises(EvalDatasetError, match="no labeled cases"):
        load_grain_cases(empty)


def test_load_candidates_parses_llm_configs(tmp_path: Path) -> None:
    path = tmp_path / "candidates.yaml"
    path.write_text(
        "candidates:\n"
        "  - name: small-local\n"
        "    provider: openai_compatible\n"
        "    base_url: http://127.0.0.1:11434/v1\n"
        "    model: qwen2.5:3b\n"
    )

    candidates = load_candidates(path)

    assert [c.name for c in candidates] == ["small-local"]
    assert candidates[0].config.model == "qwen2.5:3b"
    assert candidates[0].config.provider == "openai_compatible"


def test_load_candidates_without_list_raises(tmp_path: Path) -> None:
    path = tmp_path / "candidates.yaml"
    path.write_text("model: just-a-string\n")

    with pytest.raises(EvalDatasetError, match="expected a top-level 'candidates' list"):
        load_candidates(path)


def test_load_candidates_rejects_literal_api_key(tmp_path: Path) -> None:
    path = tmp_path / "candidates.yaml"
    path.write_text(
        "candidates:\n"
        "  - name: leaky\n"
        "    provider: openai_compatible\n"
        "    base_url: http://127.0.0.1:11434/v1\n"
        "    model: m\n"
        "    api_key_ref: sk-literal-secret\n"  # not a reference
    )

    with pytest.raises(EvalDatasetError, match="invalid llm config"):
        load_candidates(path)
