"""Tests for reports/scope.py path-derivation and visibility (AMENDMENT-user-scoped-queries-reports)."""

from __future__ import annotations

from pathlib import Path

import pytest

from canonic.exc import ReportError
from canonic.reports.scope import (
    ReportKind,
    ReportScope,
    is_visible,
    kind_from_path,
    query_path,
    report_path,
    scope_from_path,
    user_from_path,
)


def test_scope_from_global_path() -> None:
    assert scope_from_path(Path("proj/reports/global/foo.yaml")) is ReportScope.GLOBAL


def test_scope_from_user_path() -> None:
    assert scope_from_path(Path("proj/reports/user/alice/foo.yaml")) is ReportScope.USER


def test_scope_rejects_unscoped_flat_path() -> None:
    with pytest.raises(ReportError, match="unscoped or unrecognized"):
        scope_from_path(Path("proj/reports/foo.yaml"))


def test_scope_rejects_not_under_reports_dir() -> None:
    with pytest.raises(ReportError, match="not under a 'reports/' directory"):
        scope_from_path(Path("proj/other/foo.yaml"))


def test_user_from_global_path_is_none() -> None:
    assert user_from_path(Path("proj/reports/global/foo.yaml")) is None


def test_user_from_user_path() -> None:
    assert user_from_path(Path("proj/reports/user/alice/foo.yaml")) == "alice"


def test_user_from_user_queries_path() -> None:
    assert user_from_path(Path("proj/reports/user/alice/queries/foo.yaml")) == "alice"


def test_user_from_path_rejects_missing_owner_segment() -> None:
    with pytest.raises(ReportError, match="no '<id>' owner segment"):
        user_from_path(Path("proj/reports/user/foo.yaml"))


def test_kind_global_is_always_report() -> None:
    assert kind_from_path(Path("proj/reports/global/foo.yaml")) is ReportKind.REPORT


def test_kind_user_report() -> None:
    assert kind_from_path(Path("proj/reports/user/alice/foo.yaml")) is ReportKind.REPORT


def test_kind_user_query() -> None:
    assert kind_from_path(Path("proj/reports/user/alice/queries/foo.yaml")) is ReportKind.QUERY


def test_report_path_builder() -> None:
    root = Path("/proj")
    assert report_path(root, "alice", "r1") == root / "reports" / "user" / "alice" / "r1.yaml"


def test_query_path_builder() -> None:
    root = Path("/proj")
    assert query_path(root, "alice", "q1") == (
        root / "reports" / "user" / "alice" / "queries" / "q1.yaml"
    )


def test_is_visible_global_always() -> None:
    assert is_visible("bob", ReportScope.GLOBAL, None) is True


def test_is_visible_own_user_scope() -> None:
    assert is_visible("alice", ReportScope.USER, "alice") is True


def test_is_visible_not_another_users_scope() -> None:
    assert is_visible("bob", ReportScope.USER, "alice") is False
