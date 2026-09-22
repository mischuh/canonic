"""Tests for the report loader (AMENDMENT-curated-reports)."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from pydantic import ValidationError

from canonic.exc import ReportError
from canonic.reports.loader import list_reports, load_report
from canonic.reports.models import Report

if TYPE_CHECKING:
    from pathlib import Path

_VALID_REPORT_YAML = """\
id: customer_report
title: "Customer Report"
description: "Curated customer segmentation."
owner: data-team
domain: orders
sections:
  - title: "Revenue by segment"
    query: { metrics: [revenue], dimensions: [segment] }
    narrative_from: revenue-definition
  - title: "Order count"
    query: { metrics: [order_count] }
"""


def _write(tmp_path: Path, content: str, name: str = "customer_report.yaml") -> Path:
    p = tmp_path / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content)
    return p


def test_load_valid_report(tmp_path: Path) -> None:
    report = load_report(_write(tmp_path, _VALID_REPORT_YAML))
    assert report.id == "customer_report"
    assert report.title == "Customer Report"
    assert report.domain == "orders"
    assert len(report.sections) == 2
    assert report.sections[0].title == "Revenue by segment"
    assert report.sections[0].query.metrics == ["revenue"]
    assert report.sections[0].narrative_from == "revenue-definition"
    assert report.sections[1].narrative_from is None


def test_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(ReportError, match="not found"):
        load_report(tmp_path / "nope.yaml")


def test_malformed_yaml_reports_file_and_line(tmp_path: Path) -> None:
    path = _write(tmp_path, "id: [this is not\n  a valid: yaml mapping\n")
    with pytest.raises(ReportError, match="cannot parse YAML"):
        load_report(path)


def test_missing_required_field_reports_location(tmp_path: Path) -> None:
    yaml = "title: Missing id\nsections:\n  - title: s\n    query: {metrics: [revenue]}\n"
    path = _write(tmp_path, yaml)
    with pytest.raises(ReportError) as exc:
        load_report(path)
    assert str(path) in str(exc.value)


def test_report_requires_at_least_one_section() -> None:
    with pytest.raises(ValidationError):
        Report.model_validate({"id": "r", "title": "R", "sections": []})


def test_list_returns_empty_when_no_reports_dir(tmp_path: Path) -> None:
    assert list_reports(tmp_path) == []


def test_list_returns_all_sorted(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "id: b_report\ntitle: B\nsections:\n  - title: s\n    query: {metrics: [revenue]}\n",
        name="reports/global/b.yaml",
    )
    _write(
        tmp_path,
        "id: a_report\ntitle: A\nsections:\n  - title: s\n    query: {metrics: [revenue]}\n",
        name="reports/global/a.yaml",
    )
    reports = list_reports(tmp_path)
    # loaded in file-path sort order
    assert [r.report.id for r in reports] == ["a_report", "b_report"]
    assert all(r.scope.value == "global" and r.owner is None for r in reports)


def test_list_rejects_duplicate_id_in_same_namespace(tmp_path: Path) -> None:
    body = "id: dup\ntitle: T\nsections:\n  - title: s\n    query: {metrics: [revenue]}\n"
    _write(tmp_path, body, name="reports/global/one.yaml")
    _write(tmp_path, body, name="reports/global/two.yaml")
    with pytest.raises(ReportError, match="duplicate id 'dup'"):
        list_reports(tmp_path)


def test_list_allows_same_id_across_namespaces(tmp_path: Path) -> None:
    """alice and bob (and a report vs. a query) may share an id — namespaced by directory."""
    body = "id: shared\ntitle: T\nsections:\n  - title: s\n    query: {metrics: [revenue]}\n"
    _write(tmp_path, body, name="reports/global/shared.yaml")
    _write(tmp_path, body, name="reports/user/alice/shared.yaml")
    _write(tmp_path, body, name="reports/user/bob/shared.yaml")
    _write(tmp_path, body, name="reports/user/alice/queries/shared.yaml")
    reports = list_reports(tmp_path)
    assert len(reports) == 4


def test_list_rejects_unscoped_flat_layout(tmp_path: Path) -> None:
    """The pre-amendment flat reports/*.yaml layout is a clean break, not implicit global."""
    body = "id: r\ntitle: T\nsections:\n  - title: s\n    query: {metrics: [revenue]}\n"
    _write(tmp_path, body, name="reports/customer_report.yaml")
    with pytest.raises(ReportError, match="unscoped or unrecognized"):
        list_reports(tmp_path)
