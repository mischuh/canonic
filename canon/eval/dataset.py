"""Labeled ``draft`` dataset: grain-inference cases the harness scores (SPEC-E10 §7, GH-66).

Each case is a relation schema with no declared primary key plus its known-correct grain. The
case builds a real :class:`~canon.connectors.base.RelationSchema` so the harness feeds the
*production* drafter (:class:`~canon.runtime.drafter.RuntimeLLMDrafter`) exactly as E4 would —
the baseline measures real behavior, not a re-implemented prompt.
"""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import BaseModel, ConfigDict, ValidationError

from canon.connectors.base import AcquisitionTier, ColumnInfo, RelationSchema
from canon.exc import EvalDatasetError

__all__ = ["GrainCase", "default_dataset_path", "load_grain_cases"]


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


def default_dataset_path() -> Path:
    """Path to the shipped labeled ``draft`` set, used when the CLI gets no ``--dataset``."""
    return Path(__file__).parent / "datasets" / "draft_grain.jsonl"


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
