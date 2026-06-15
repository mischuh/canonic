"""Load and list contract YAML files — metrics, guardrails, assertions (SPEC-E15 §2.2–2.5)."""

from __future__ import annotations

import io
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ValidationError
from ruamel.yaml import YAML

from canon.contracts.models import (
    Assertion,
    ContractValidationError,
    Guardrail,
    MetricBinding,
    Status,
)
from canon.exc import ContractError

if TYPE_CHECKING:
    from collections.abc import Iterable
    from pathlib import Path

__all__ = [
    "contracts_dir_scaffold",
    "dump_assertion",
    "dump_guardrail",
    "dump_metric_binding",
    "load_assertions",
    "load_guardrails",
    "load_metric_bindings",
]

_METRICS_DIR = "contracts/metrics"
_GUARDRAILS_DIR = "contracts/guardrails"
_ASSERTIONS_DIR = "contracts/assertions"


def _line_for_path(raw: Any, path: Iterable[str | int]) -> int | None:
    """Best-effort 1-based line for a YAML path, walking ruamel's `.lc` data."""
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
    raise ContractError(f"{where}: {message}")


def _load_one[M: BaseModel](path: Path, model_cls: type[M]) -> M:
    """Load and validate one contract YAML file, raising ContractError with file:line."""
    if not path.exists():
        raise ContractError(f"contract file not found: {path}")

    yaml = YAML()
    try:
        with open(path) as f:
            raw: Any = yaml.load(f) or {}
    except Exception as exc:  # noqa: BLE001
        raise ContractError(f"{path}: cannot parse YAML: {exc}") from exc

    try:
        return model_cls.model_validate(raw)
    except ValidationError as exc:
        err = exc.errors()[0]
        located = err.get("ctx", {}).get("error")
        if isinstance(located, ContractValidationError):
            _raise_located(path, raw, located.path, str(located))
        loc = err["loc"]
        msg = err["msg"]
        suffix = " → ".join(str(p) for p in loc)
        message = f"{suffix}: {msg}" if suffix else msg
        _raise_located(path, raw, loc, message)
        raise AssertionError("unreachable") from exc  # _raise_located always raises


def load_metric_bindings(project_root: Path) -> list[MetricBinding]:
    """Load every contracts/metrics/**/*.yaml, checking for duplicate active names/aliases.

    Raises ContractError naming both file paths if two active bindings share a metric
    name or alias (SPEC-E15 §2.2 ambiguity rule, AC1).
    """
    base = project_root / _METRICS_DIR
    if not base.is_dir():
        return []

    loaded: list[tuple[Path, MetricBinding]] = [
        (p, _load_one(p, MetricBinding)) for p in sorted(base.rglob("*.yaml"))
    ]

    # within-surface duplicate check: active bindings only
    seen_names: dict[str, Path] = {}
    for path, binding in loaded:
        if binding.status is not Status.ACTIVE:
            continue
        candidates = [binding.metric, *binding.aliases]
        for name in candidates:
            if name in seen_names:
                raise ContractError(
                    f"duplicate active metric name/alias {name!r}: {seen_names[name]} and {path}"
                )
            seen_names[name] = path

    return [b for _, b in loaded]


def load_guardrails(project_root: Path) -> list[Guardrail]:
    """Load every contracts/guardrails/**/*.yaml (skips finality-* stubs in P0)."""
    base = project_root / _GUARDRAILS_DIR
    if not base.is_dir():
        return []
    paths = [p for p in sorted(base.rglob("*.yaml")) if not p.name.startswith("finality-")]
    return [_load_one(p, Guardrail) for p in paths]


def load_assertions(project_root: Path) -> list[Assertion]:
    """Load every contracts/assertions/**/*.yaml."""
    base = project_root / _ASSERTIONS_DIR
    if not base.is_dir():
        return []
    return [_load_one(p, Assertion) for p in sorted(base.rglob("*.yaml"))]


def contracts_dir_scaffold(project_root: Path) -> None:
    """Create contracts/{metrics,guardrails,assertions} dirs if absent."""
    for subdir in (_METRICS_DIR, _GUARDRAILS_DIR, _ASSERTIONS_DIR):
        (project_root / subdir).mkdir(parents=True, exist_ok=True)


def _dump(obj: BaseModel) -> str:
    yaml = YAML()
    yaml.default_flow_style = False
    buffer = io.StringIO()
    yaml.dump(obj.model_dump(mode="json"), buffer)
    return buffer.getvalue()


def dump_metric_binding(binding: MetricBinding) -> str:
    """Serialize a MetricBinding to YAML such that load→dump→load round-trips."""
    return _dump(binding)


def dump_guardrail(guardrail: Guardrail) -> str:
    """Serialize a Guardrail to YAML such that load→dump→load round-trips."""
    return _dump(guardrail)


def dump_assertion(assertion: Assertion) -> str:
    """Serialize an Assertion to YAML such that load→dump→load round-trips."""
    return _dump(assertion)
