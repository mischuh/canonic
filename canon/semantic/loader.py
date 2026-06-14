"""Load, list, and dump semantics/*.yaml files (SPEC-E5 §2.1, §7, §8)."""

from __future__ import annotations

import io
from typing import TYPE_CHECKING, Any

from pydantic import ValidationError
from ruamel.yaml import YAML

from canon.exc import SemanticSourceError
from canon.semantic.models import SemanticSource, SemanticValidationError

if TYPE_CHECKING:
    from collections.abc import Iterable
    from pathlib import Path

__all__ = ["dump_semantic_source", "list_semantic_sources", "load_semantic_source"]

_SEMANTICS_DIR = "semantics"


def _line_for_path(raw: Any, path: Iterable[str | int]) -> int | None:
    """Best-effort 1-based line for a YAML path, walking ruamel's `.lc` data.

    Returns the deepest resolvable line, or None if nothing could be located.
    """
    node = raw
    line: int | None = None
    for key in path:
        lc = getattr(node, "lc", None)
        data = getattr(lc, "data", None)
        if not isinstance(data, dict) or key not in data:
            break
        line = data[key][0]  # ruamel rows are 0-based
        try:
            node = node[key]
        except (KeyError, IndexError, TypeError):
            break
    return None if line is None else line + 1


def _raise_located(path: Path, raw: Any, loc: Iterable[str | int], message: str) -> None:
    line = _line_for_path(raw, loc)
    where = f"{path}:{line}" if line is not None else str(path)
    raise SemanticSourceError(f"{where}: {message}")


def load_semantic_source(path: Path) -> SemanticSource:
    """Load and validate one semantic source YAML, raising SemanticSourceError.

    The error message carries ``file:line`` for the offending node where it can
    be located (SPEC-E5 §7).
    """
    if not path.exists():
        raise SemanticSourceError(f"semantic source not found: {path}")

    yaml = YAML()  # round-trip mode: loaded nodes carry `.lc` line/col data
    try:
        with open(path) as f:
            raw: Any = yaml.load(f) or {}
    except Exception as exc:  # noqa: BLE001 — any parse failure is a source error
        raise SemanticSourceError(f"{path}: cannot parse YAML: {exc}") from exc

    try:
        return SemanticSource.model_validate(raw)
    except ValidationError as exc:
        err = exc.errors()[0]
        located = err.get("ctx", {}).get("error")
        if isinstance(located, SemanticValidationError):
            _raise_located(path, raw, located.path, str(located))
        loc = err["loc"]
        msg = err["msg"]
        suffix = " → ".join(str(p) for p in loc)
        message = f"{suffix}: {msg}" if suffix else msg
        _raise_located(path, raw, loc, message)
        raise AssertionError("unreachable") from exc  # _raise_located always raises


def list_semantic_sources(project_root: Path) -> list[SemanticSource]:
    """Load every semantics/**/*.yaml under project_root, sorted for determinism."""
    base = project_root / _SEMANTICS_DIR
    if not base.is_dir():
        return []
    return [load_semantic_source(p) for p in sorted(base.rglob("*.yaml"))]


def dump_semantic_source(source: SemanticSource) -> str:
    """Serialize a semantic source to YAML such that load→dump→load round-trips."""
    yaml = YAML()
    yaml.default_flow_style = False
    buffer = io.StringIO()
    yaml.dump(source.model_dump(mode="json"), buffer)
    return buffer.getvalue()
