"""Candidate model configs for the baseline harness (SPEC-E10 §7, GH-66).

A candidates file lists the local (or hosted ``openai_compatible``) models to put through the
harness. Each entry is a friendly ``name`` plus the same fields as a ``canonic.yaml`` ``llm`` block,
parsed into an :class:`~canonic.config.LLMConfig` so the harness drives the exact production path.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict, ValidationError
from ruamel.yaml import YAML
from ruamel.yaml.error import YAMLError

from canonic.config import LLMConfig
from canonic.exc import EvalDatasetError

if TYPE_CHECKING:
    from pathlib import Path

__all__ = ["NamedCandidate", "load_candidates"]


class NamedCandidate(BaseModel):
    """A labeled candidate: an operator-facing name and its resolved ``llm`` config."""

    model_config = ConfigDict(frozen=True)

    name: str
    config: LLMConfig


def load_candidates(path: Path) -> list[NamedCandidate]:
    """Load candidate model configs from a YAML file.

    Expected shape::

        candidates:
          - name: small-local
            provider: openai_compatible
            base_url: http://127.0.0.1:11434/v1
            model: qwen2.5:3b
            api_key_ref: env:CANONIC_LLM_KEY   # optional; local servers need none

    Raises:
        EvalDatasetError: The file is missing, not valid YAML, has no ``candidates`` list, or an
            entry does not parse into a name + :class:`LLMConfig`.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise EvalDatasetError(f"cannot read candidates {path}: {exc}") from exc

    try:
        data = YAML(typ="safe").load(text)
    except YAMLError as exc:
        raise EvalDatasetError(f"{path}: invalid YAML: {exc}") from exc

    if not isinstance(data, dict) or not isinstance(data.get("candidates"), list):
        raise EvalDatasetError(f"{path}: expected a top-level 'candidates' list")

    candidates: list[NamedCandidate] = []
    for index, entry in enumerate(data["candidates"], start=1):
        if not isinstance(entry, dict):
            raise EvalDatasetError(f"{path}: candidate #{index} is not a mapping")
        spec: dict[str, Any] = dict(entry)
        name = spec.pop("name", None)
        if not name:
            raise EvalDatasetError(f"{path}: candidate #{index} is missing 'name'")
        try:
            config = LLMConfig.model_validate(spec)
        except ValidationError as exc:
            detail = exc.errors()[0]["msg"] if exc.errors() else str(exc)
            raise EvalDatasetError(
                f"{path}: candidate {name!r} has an invalid llm config: {detail}"
            ) from exc
        candidates.append(NamedCandidate(name=name, config=config))

    if not candidates:
        raise EvalDatasetError(f"{path}: no candidates found")
    return candidates
