"""Curated + personal report layer: typed models and YAML IO for reports/**/*.yaml."""

from __future__ import annotations

from canonic.reports.loader import LoadedReport, dump_report, list_reports, load_report
from canonic.reports.models import Report, ReportSection
from canonic.reports.scope import ReportKind, ReportScope

__all__ = [
    "LoadedReport",
    "Report",
    "ReportKind",
    "ReportScope",
    "ReportSection",
    "dump_report",
    "list_reports",
    "load_report",
]
