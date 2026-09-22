"""Path-based scope/kind derivation for reports/** (AMENDMENT-user-scoped-queries-reports).

Mirrors ``canonic/knowledge/loader.py`` + ``canonic/knowledge/scope.py`` (SPEC-E6 §4): scope and
owner are derived from the file path, never read from the file itself, and visibility is strictly
additive — a user sees ``global/`` plus their own ``user/<id>/``, never another user's.

A report file lives in one of two shapes:

- ``reports/global/<id>.yaml`` — data-team maintained, PR-reviewed.
- ``reports/user/<id>/<report-id>.yaml`` — a personal composed report.
- ``reports/user/<id>/queries/<query-id>.yaml`` — a personal saved query.

The ``queries/`` sub-path is what makes a saved query and a composed report physically
un-reachable from each other's delete capability (S23): ``kind_from_path`` is derived purely from
path shape, not from file content.
"""

from __future__ import annotations

from enum import StrEnum
from typing import TYPE_CHECKING

from canonic.exc import ReportError

if TYPE_CHECKING:
    from pathlib import Path

__all__ = [
    "ReportKind",
    "ReportScope",
    "is_visible",
    "kind_from_path",
    "query_path",
    "report_path",
    "scope_from_path",
    "user_from_path",
]

_REPORTS_DIR = "reports"
_QUERIES_DIR = "queries"


class ReportScope(StrEnum):
    """Path-defined visibility scope of a report/query file (mirrors ``KnowledgeScope``)."""

    GLOBAL = "global"  # reports/global/ — shared, PR-reviewed
    USER = "user"  # reports/user/<id>/ — personal, self-service


class ReportKind(StrEnum):
    """What a report-scoped file *is*, derived purely from its path shape."""

    REPORT = "report"  # reports/global/<id>.yaml or reports/user/<id>/<report-id>.yaml
    QUERY = "query"  # reports/user/<id>/queries/<query-id>.yaml


def _reports_relative_parts(path: Path) -> tuple[str, ...]:
    parts = path.parts
    try:
        i = parts.index(_REPORTS_DIR)
    except ValueError:
        raise ReportError(
            f"{path}: not under a '{_REPORTS_DIR}/' directory; cannot derive scope"
        ) from None
    return parts[i + 1 :]


def scope_from_path(path: Path) -> ReportScope:
    """Derive a report/query's scope from its path.

    ``reports/global/…`` → GLOBAL, ``reports/user/<id>/…`` → USER. Raises ``ReportError`` for an
    unscoped ``reports/<file>.yaml`` (the pre-amendment flat layout) or any other unrecognized
    shape — there is no implicit-global fallback (clean-break migration).
    """
    rel = _reports_relative_parts(path)
    segment = rel[0] if rel else ""
    try:
        return ReportScope(segment)
    except ValueError:
        raise ReportError(
            f"{path}: unscoped or unrecognized report path; expected "
            f"'{_REPORTS_DIR}/global/<id>.yaml' or "
            f"'{_REPORTS_DIR}/user/<id>/[queries/]<id>.yaml'"
        ) from None


def user_from_path(path: Path) -> str | None:
    """Owner id of a USER-scoped report/query (``reports/user/<id>/…``), ``None`` for GLOBAL.

    Raises ``ReportError`` if a ``user/`` path omits the owner directory.
    """
    if scope_from_path(path) is ReportScope.GLOBAL:
        return None
    rel = _reports_relative_parts(path)
    # rel[0] is "user"; rel[1] must be the owner directory, not the filename.
    if len(rel) < 3:
        raise ReportError(
            f"{path}: user report/query has no '<id>' owner segment; expected "
            f"'{_REPORTS_DIR}/user/<id>/[queries/]<id>.yaml'"
        )
    return rel[1]


def kind_from_path(path: Path) -> ReportKind:
    """Whether *path* is a composed report or an atomic saved query.

    A ``QUERY`` is exactly ``reports/user/<id>/queries/<file>.yaml`` — one directory deeper than a
    personal report, and only under ``user/`` (there is no global saved-query concept, v1).
    """
    if scope_from_path(path) is ReportScope.GLOBAL:
        return ReportKind.REPORT
    rel = _reports_relative_parts(path)
    # rel = ("user", "<id>", ..., "<file>.yaml")
    if len(rel) == 4 and rel[2] == _QUERIES_DIR:
        return ReportKind.QUERY
    if len(rel) == 3:
        return ReportKind.REPORT
    raise ReportError(
        f"{path}: unrecognized report/query path shape; expected "
        f"'{_REPORTS_DIR}/user/<id>/<report-id>.yaml' or "
        f"'{_REPORTS_DIR}/user/<id>/{_QUERIES_DIR}/<query-id>.yaml'"
    )


def report_path(project_root: Path, user: str, report_id: str) -> Path:
    """The on-disk path for a personal composed report (write-side path builder)."""
    return project_root / _REPORTS_DIR / "user" / user / f"{report_id}.yaml"


def query_path(project_root: Path, user: str, query_id: str) -> Path:
    """The on-disk path for a personal saved query (write-side path builder)."""
    return project_root / _REPORTS_DIR / "user" / user / _QUERIES_DIR / f"{query_id}.yaml"


def is_visible(requesting_user: str, scope: ReportScope, owner: str | None) -> bool:
    """Whether *requesting_user* may see a report/query with the given ``(scope, owner)``.

    GLOBAL is always visible; USER only to its own owner (SPEC-E6 §4, applied to ``reports/``).
    """
    return scope is ReportScope.GLOBAL or owner == requesting_user
