"""Labeled datasets for the baseline harness: grain-inference and contradiction-resolution cases.

Each grain case is a relation schema with no declared primary key plus its known-correct grain.
Each reconcile case is a pair of conflicting proposals plus the expected winner index.
Both feed the *production* drafter exactly as E4 would — the baseline measures real behavior.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, ValidationError

from canonic.connectors.base import AcquisitionTier, ColumnInfo, RelationSchema
from canonic.exc import EvalDatasetError

__all__ = [
    "GrainCase",
    "ReconcileCase",
    "default_dataset_path",
    "default_reconcile_dataset_path",
    "load_grain_cases",
    "load_reconcile_cases",
]


class GrainCase(BaseModel):
    """A labeled grain-inference case: a relation schema and its known-correct grain."""

    model_config = ConfigDict(frozen=True)

    relation: str
    columns: list[ColumnInfo]
    expected_grain: list[str]

    def to_schema(self) -> RelationSchema:
        """Build the connector schema the drafter consumes (no declared primary key)."""
        return RelationSchema(
            connection="eval",
            relation=self.relation,
            kind="table",
            columns=self.columns,
            acquisition_tier=AcquisitionTier.SAMPLE,
        )


class ReconcileCase(BaseModel):
    """A labeled contradiction-resolution case: two proposals and the correct winner index."""

    model_config = ConfigDict(frozen=True)

    target: str
    proposals: list[dict[str, Any]]
    expected_winner: int


def default_dataset_path() -> Path:
    """Path to the shipped labeled ``draft`` set, used when the CLI gets no ``--dataset``."""
    return Path(__file__).parent / "datasets" / "draft_grain.jsonl"


def default_reconcile_dataset_path() -> Path:
    """Path to the shipped labeled ``reconcile`` set."""
    return Path(__file__).parent / "datasets" / "reconcile_contradictions.jsonl"


def load_grain_cases(path: Path) -> list[GrainCase]:
    """Load labeled grain cases from a JSONL file (one case object per line).

    Raises:
        EvalDatasetError: The file is missing, a line is not valid JSON, or a line does not
            satisfy :class:`GrainCase` — the message carries the file and 1-based line number.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise EvalDatasetError(f"cannot read dataset {path}: {exc}") from exc

    cases: list[GrainCase] = []
    for lineno, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError as exc:
            raise EvalDatasetError(f"{path}:{lineno}: invalid JSON: {exc}") from exc
        try:
            cases.append(GrainCase.model_validate(payload))
        except ValidationError as exc:
            detail = exc.errors()[0]["msg"] if exc.errors() else str(exc)
            raise EvalDatasetError(f"{path}:{lineno}: invalid grain case: {detail}") from exc

    if not cases:
        raise EvalDatasetError(f"{path}: no labeled cases found")
    return cases


def load_reconcile_cases(path: Path) -> list[ReconcileCase]:
    """Load labeled reconcile cases from a JSONL file (one case object per line).

    Raises:
        EvalDatasetError: The file is missing, a line is not valid JSON, or a line does not
            satisfy :class:`ReconcileCase` — the message carries the file and 1-based line number.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise EvalDatasetError(f"cannot read dataset {path}: {exc}") from exc

    cases: list[ReconcileCase] = []
    for lineno, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError as exc:
            raise EvalDatasetError(f"{path}:{lineno}: invalid JSON: {exc}") from exc
        try:
            cases.append(ReconcileCase.model_validate(payload))
        except ValidationError as exc:
            detail = exc.errors()[0]["msg"] if exc.errors() else str(exc)
            raise EvalDatasetError(f"{path}:{lineno}: invalid reconcile case: {detail}") from exc

    if not cases:
        raise EvalDatasetError(f"{path}: no labeled cases found")
    return cases
