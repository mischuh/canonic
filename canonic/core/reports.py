"""Curated report capabilities: list_reports / run_report (AMENDMENT-curated-reports).

A report names an ordered sequence of calls into capabilities that already exist —
``core.query`` per section, optionally ``core.read_page`` for a narrative. This service
introduces no new execution semantics and no new authority: it is a deterministic loop,
composing :class:`~canonic.core.query.QueryService` and
:class:`~canonic.core.knowledge.KnowledgeService`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from canonic.core.models import (
    ReportNarrative,
    ReportRunResult,
    ReportSectionResult,
    ReportSummary,
)
from canonic.exc import CanonicError, ReportError, ReportNotFound
from canonic.mcp.errors import error_payload
from canonic.reports.loader import list_reports as load_reports

if TYPE_CHECKING:
    from datetime import datetime

    from canonic.compiler import SemanticQuery
    from canonic.core.context import ServiceContext
    from canonic.core.knowledge import KnowledgeService
    from canonic.core.query import QueryService
    from canonic.reports.models import Report, ReportSection


class ReportService:
    """List committed reports and run their sections through the query capability."""

    def __init__(
        self, ctx: ServiceContext, query: QueryService, knowledge: KnowledgeService
    ) -> None:
        self._ctx = ctx
        self._query = query
        self._knowledge = knowledge

    def _load_reports(self) -> list[Report]:
        if self._ctx.project_root is None:
            return []
        return load_reports(self._ctx.project_root)

    def list_reports(self, domain: str | None = None) -> list[ReportSummary]:
        """Directory listing of committed reports — no execution, no per-section detail.

        ``domain`` filters to reports declaring a matching ``domain`` field, the same
        grouping convention as ``get_overview(domain?)``.
        """
        reports = sorted(self._load_reports(), key=lambda r: r.id)
        return [
            ReportSummary(
                id=r.id, title=r.title, description=r.description, owner=r.owner, domain=r.domain
            )
            for r in reports
            if domain is None or r.domain == domain
        ]

    def _find_report(self, report_id: str) -> Report:
        for report in self._load_reports():
            if report.id == report_id:
                return report
        raise ReportNotFound(f"report {report_id!r} matches no committed report")

    def _effective_query(
        self, section: ReportSection, report: Report, as_of: datetime | None
    ) -> SemanticQuery:
        context = section.query.context if section.query.context is not None else report.context
        section_as_of = section.query.as_of if section.query.as_of is not None else as_of
        return section.query.model_copy(update={"context": context, "as_of": section_as_of})

    async def run_report(
        self,
        report_id: str,
        *,
        as_of: datetime | None = None,
        user: str | None = None,
        caller: str | None = None,
    ) -> ReportRunResult:
        """Run every section of a committed report through ``core.query``, in order (S16).

        A failing section does not abort the run (S17): it resolves to a structured
        ``{code, message, candidates?}`` error in place of a result, and the call as a
        whole never raises for a per-section failure. Only an unknown ``report_id``
        raises (:class:`~canonic.exc.ReportNotFound`, reusing the ``unresolved`` wire
        code — no new error code).
        """
        report = self._find_report(report_id)

        sections: list[ReportSectionResult] = []
        for section in report.sections:
            effective = self._effective_query(section, report, as_of)
            try:
                result = await self._query.query(effective, caller=caller)
            except CanonicError as exc:
                sections.append(ReportSectionResult(title=section.title, error=error_payload(exc)))
                continue

            narrative: ReportNarrative | None = None
            if section.narrative_from is not None:
                try:
                    page = self._knowledge.read_knowledge_page(section.narrative_from, user=user)
                except (KeyError, PermissionError) as exc:
                    error: dict[str, Any] = {"code": "unresolved", "message": str(exc)}
                    sections.append(ReportSectionResult(title=section.title, error=error))
                    continue
                narrative = ReportNarrative(
                    page_id=page["page_id"], body=page["body"], meta=page["meta"]
                )

            sections.append(
                ReportSectionResult(title=section.title, result=result, narrative=narrative)
            )

        return ReportRunResult(report_id=report.id, sections=sections)

    def validate_reports(self) -> None:
        """Validate every committed report against the live semantic/knowledge layer.

        Each section's query must compile (dry-run, no execution) and each
        ``narrative_from`` must resolve to an existing knowledge-page id. Raises
        :class:`~canonic.exc.ReportError` naming the report id and section index on
        the first failure (S18) — never a silent skip.
        """
        known_pages = self._known_knowledge_page_ids()
        for report in self._load_reports():
            for index, section in enumerate(report.sections):
                effective = self._effective_query(section, report, as_of=None)
                try:
                    self._query.compile_query(effective)
                except CanonicError as exc:
                    raise ReportError(
                        f"report {report.id!r} section {index}: query does not compile: {exc}"
                    ) from exc
                if section.narrative_from is not None and section.narrative_from not in known_pages:
                    raise ReportError(
                        f"report {report.id!r} section {index}: narrative_from "
                        f"{section.narrative_from!r} does not resolve to an existing "
                        f"knowledge page"
                    )

    def _known_knowledge_page_ids(self) -> set[str]:
        if self._ctx.project_root is None:
            return set()
        knowledge_root = self._ctx.project_root / "knowledge"
        if not knowledge_root.exists():
            return set()
        from canonic.knowledge import load_knowledge_page

        return {load_knowledge_page(p).id for p in sorted(knowledge_root.rglob("*.md"))}
