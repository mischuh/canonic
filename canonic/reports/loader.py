"""Load and list reports/**/*.yaml files.

Layout (AMENDMENT-user-scoped-queries-reports): ``reports/global/*.yaml`` (data-team maintained,
PR-reviewed) and ``reports/user/<id>/*.yaml`` (personal composed reports) plus
``reports/user/<id>/queries/*.yaml`` (personal saved queries). Scope, owner, and kind are derived
from each file's path (:mod:`canonic.reports.scope`), never read from the file itself.
"""

from __future__ import annotations

import io
from pathlib import Path  # noqa: TC003 — Pydantic resolves field annotations at runtime
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict, ValidationError
from ruamel.yaml import YAML

from canonic.exc import ReportError
from canonic.reports.models import Report
from canonic.reports.scope import (
    ReportKind,
    ReportScope,
    kind_from_path,
    scope_from_path,
    user_from_path,
)

if TYPE_CHECKING:
    from collections.abc import Iterable

__all__ = ["LoadedReport", "dump_report", "list_reports", "load_report"]

_REPORTS_DIR = "reports"


class LoadedReport(BaseModel):
    """A parsed report/query paired with the scope/owner/kind derived from its file path."""

    model_config = ConfigDict(frozen=True)

    report: Report
    path: Path
    scope: ReportScope
    owner: str | None
    kind: ReportKind


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
    """Load and validate one report/query YAML, raising ReportError.

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


def dump_report(report: Report) -> str:
    """Serialize a report/query to its on-disk YAML form (write side for personal writes).

    ``exclude_none`` keeps the common case (no ``description``/``domain``/``narrative_from``)
    readable; a saved query round-trips through :func:`load_report` unchanged.
    """
    data = report.model_dump(mode="json", exclude_none=True)
    yaml = YAML()
    yaml.default_flow_style = False
    buffer = io.StringIO()
    yaml.dump(data, buffer)
    return buffer.getvalue()


def list_reports(project_root: Path) -> list[LoadedReport]:
    """Load every reports/**/*.yaml under project_root, sorted for determinism.

    Every file must sit under ``reports/global/`` or ``reports/user/<id>/`` (including
    ``reports/user/<id>/queries/``) — an unscoped ``reports/<file>.yaml`` (the pre-amendment flat
    layout) raises ``ReportError`` naming the expected layout; there is no implicit-global
    fallback.

    Raises ``ReportError`` on a duplicate ``id`` *within the same namespace*: among
    ``global/`` reports, among one user's reports, or among one user's queries. Ids are namespaced
    by directory path, so alice and bob (or a report and a same-named query) may share an id.
    Returns ``[]`` when the ``reports/`` directory does not exist.
    """
    base = project_root / _REPORTS_DIR
    if not base.is_dir():
        return []

    loaded: list[LoadedReport] = []
    seen_at: dict[tuple[ReportScope, str | None, ReportKind, str], Path] = {}
    for path in sorted(base.rglob("*.yaml")):
        report = load_report(path)
        scope = scope_from_path(path)
        owner = user_from_path(path)
        kind = kind_from_path(path)
        namespace = (scope, owner, kind, report.id)
        if namespace in seen_at:
            raise ReportError(
                f"{path}: duplicate id {report.id!r} in this namespace "
                f"(already defined at {seen_at[namespace]})"
            )
        seen_at[namespace] = path
        loaded.append(LoadedReport(report=report, path=path, scope=scope, owner=owner, kind=kind))
    return loaded
