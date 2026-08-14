"""Curated report layer: typed models and YAML IO for reports/*.yaml."""

from __future__ import annotations

from canonic.reports.loader import list_reports, load_report
from canonic.reports.models import Report, ReportSection

__all__ = [
    "Report",
    "ReportSection",
    "list_reports",
    "load_report",
]
