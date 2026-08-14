"""Load and list reports/*.yaml files (AMENDMENT-curated-reports)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from pydantic import ValidationError
from ruamel.yaml import YAML

from canonic.exc import ReportError
from canonic.reports.models import Report

if TYPE_CHECKING:
    from collections.abc import Iterable
    from pathlib import Path

__all__ = ["list_reports", "load_report"]

_REPORTS_DIR = "reports"


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
    raise ReportError(f"{where}: {message}")


def load_report(path: Path) -> Report:
    """Load and validate one report YAML, raising ReportError.

    The error message carries ``file:line`` for the offending node where it can be
    located, matching ``load_semantic_source``/``_load_one`` (contracts).
    """
    if not path.exists():
        raise ReportError(f"report not found: {path}")

    yaml = YAML()  # round-trip mode: loaded nodes carry `.lc` line/col data
    try:
        with open(path) as f:
            raw: Any = yaml.load(f) or {}
    except Exception as exc:  # noqa: BLE001 — any parse failure is a report error
        raise ReportError(f"{path}: cannot parse YAML: {exc}") from exc

    try:
        return Report.model_validate(raw)
    except ValidationError as exc:
        err = exc.errors()[0]
        loc = err["loc"]
        msg = err["msg"]
        suffix = " → ".join(str(p) for p in loc)
        message = f"{suffix}: {msg}" if suffix else msg
        _raise_located(path, raw, loc, message)
        raise AssertionError("unreachable") from exc  # _raise_located always raises


def list_reports(project_root: Path) -> list[Report]:
    """Load every reports/*.yaml under project_root, sorted for determinism.

    Raises ``ReportError`` if two reports share the same ``id`` — ids must be unique
    across the whole project, mirroring the duplicate-name check in
    ``list_semantic_sources``/``load_metric_bindings``. Returns ``[]`` when the
    ``reports/`` directory does not exist.
    """
    base = project_root / _REPORTS_DIR
    if not base.is_dir():
        return []

    reports: list[Report] = []
    seen_at: dict[str, Path] = {}
    for path in sorted(base.rglob("*.yaml")):
        report = load_report(path)
        if report.id in seen_at:
            raise ReportError(
                f"{path}: duplicate report id {report.id!r} "
                f"(already defined at {seen_at[report.id]})"
            )
        seen_at[report.id] = path
        reports.append(report)
    return reports
