"""Curated + personal report capabilities: list_reports / run_report.

A report names an ordered sequence of calls into capabilities that already exist —
``core.query`` per section, optionally ``core.read_page`` for a narrative. This service
introduces no new execution semantics and no new authority: it is a deterministic loop,
composing :class:`~canonic.core.query.QueryService` and
:class:`~canonic.core.knowledge.KnowledgeService`.

Scope-aware per AMENDMENT-user-scoped-queries-reports: ``list_reports``/``run_report`` see
``global/`` plus the caller's own ``user/<id>/`` (never another user's, never ``queries/`` —
S20 AC2, S24 AC1), the same visibility rule E6 §4 already applies to knowledge pages.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from canonic.contracts.principal import SYSTEM_PRINCIPAL
from canonic.core.models import (
    ReportNarrative,
    ReportRunResult,
    ReportSectionInfo,
    ReportSectionResult,
    ReportStructure,
    ReportSummary,
)
from canonic.exc import CanonicError, ReportError, ReportNotFound
from canonic.mcp.errors import error_payload
from canonic.reports.loader import list_reports as load_reports
from canonic.reports.scope import ReportKind, ReportScope, is_visible

if TYPE_CHECKING:
    from datetime import datetime

    from canonic.compiler import SemanticQuery
    from canonic.contracts.principal import Principal
    from canonic.core.context import ServiceContext
    from canonic.core.knowledge import KnowledgeService
    from canonic.core.query import QueryService
    from canonic.reports.loader import LoadedReport
    from canonic.reports.models import Report, ReportSection

_ANONYMOUS_USER = "anonymous"


def _scope_label(scope: ReportScope, owner: str | None) -> str:
    return "global" if scope is ReportScope.GLOBAL else f"user:{owner}"


class ReportService:
    """List committed reports and run their sections through the query capability."""

    def __init__(
        self, ctx: ServiceContext, query: QueryService, knowledge: KnowledgeService
    ) -> None:
        self._ctx = ctx
        self._query = query
        self._knowledge = knowledge

    def _load_all(self) -> list[LoadedReport]:
        """Every report/query file (both scopes) — unfiltered. Used by ``validate_reports``."""
        if self._ctx.project_root is None:
            return []
        return load_reports(self._ctx.project_root)

    def _visible(self, user: str | None) -> list[LoadedReport]:
        """Everything (reports + queries) visible to *user*: ``global/`` + their own scope."""
        requesting_user = user or _ANONYMOUS_USER
        return [
            loaded
            for loaded in self._load_all()
            if is_visible(requesting_user, loaded.scope, loaded.owner)
        ]

    def _visible_reports(self, user: str | None) -> list[LoadedReport]:
        """Reports (never ``queries/``) visible to *user*: ``global/`` + their own scope."""
        return [entry for entry in self._visible(user) if entry.kind is ReportKind.REPORT]

    def list_reports(
        self, domain: str | None = None, *, user: str | None = None
    ) -> list[ReportSummary]:
        """Directory listing of reports visible to *user*: ``global/`` + their own (S24 AC1).

        Never includes ``queries/`` content (S20 AC2). ``domain`` filters to reports declaring a
        matching ``domain`` field, the same grouping convention as ``get_overview(domain?)``.
        """
        loaded = sorted(self._visible_reports(user), key=lambda entry: entry.report.id)
        return [
            ReportSummary(
                id=entry.report.id,
                title=entry.report.title,
                description=entry.report.description,
                owner=entry.report.owner,
                domain=entry.report.domain,
                scope=_scope_label(entry.scope, entry.owner),
            )
            for entry in loaded
            if domain is None or entry.report.domain == domain
        ]

    def _find_report(self, report_id: str, *, user: str | None = None) -> Report:
        """Resolve *report_id* to a runnable ``Report`` — a committed report or a saved query.

        ``run_report`` deliberately spans both namespaces (S19 AC2: a saved query is runnable
        by its own id, the same way a report is), unlike ``list_reports``/the delete
        capabilities, which stay namespace-strict (S20 AC2, S23 AC2).
        """
        for entry in self._visible(user):
            if entry.report.id == report_id:
                return entry.report
        raise ReportNotFound(f"report {report_id!r} matches no committed report")

    def _effective_query(
        self,
        section: ReportSection,
        report: Report,
        as_of: datetime | None,
        filters: list[str] | None = None,
    ) -> SemanticQuery:
        context = section.query.context if section.query.context is not None else report.context
        section_as_of = section.query.as_of if section.query.as_of is not None else as_of
        combined_filters = [*section.query.filters, *(filters or [])]
        return section.query.model_copy(
            update={"context": context, "as_of": section_as_of, "filters": combined_filters}
        )

    async def run_report(
        self,
        report_id: str,
        *,
        as_of: datetime | None = None,
        filters: list[str] | None = None,
        user: str | None = None,
        caller: str | None = None,
        principal: Principal | None = None,
    ) -> ReportRunResult:
        """Run every section of a committed report through ``core.query``, in order (S16).

        A failing section does not abort the run (S17): it resolves to a structured
        ``{code, message, candidates?}`` error in place of a result, and the call as a
        whole never raises for a per-section failure. An unknown ``report_id`` — including
        one that resolves to a saved query, another user's personal report, or simply
        nothing (:class:`~canonic.exc.ReportNotFound`, reusing the ``unresolved`` wire
        code — no new error code) — raises the same way (S19 AC3, S23 AC2).

        ``filters`` are caller-supplied predicate strings (same shape as
        ``SemanticQuery.filters``) applied additively — AND-ed onto every section's own
        filters, never replacing them. They can narrow a report run but never widen or
        override the caller's actual tenant scope: ``principal`` (bound by the adapter from
        a verified token, never from ``filters``) flows into every section's ``query()``
        call and is injected as a separate AST node the compiler binds independently
        (SPEC-E12 §5, S14 AC1) — so a caller-supplied ``merchant_id = '...'`` filter can
        only ever be a redundant, narrower restatement of it, never a substitute.
        """
        report = self._find_report(report_id, user=user)

        sections: list[ReportSectionResult] = []
        for section in report.sections:
            effective = self._effective_query(section, report, as_of, filters)
            try:
                result = await self._query.query(effective, caller=caller, principal=principal)
            except CanonicError as exc:
                sections.append(
                    ReportSectionResult(
                        id=section.id, title=section.title, error=error_payload(exc)
                    )
                )
                continue

            narrative: ReportNarrative | None = None
            if section.narrative_from is not None:
                try:
                    page = self._knowledge.read_knowledge_page(
                        section.narrative_from, user=user, principal=principal
                    )
                except (KeyError, PermissionError) as exc:
                    error: dict[str, Any] = {"code": "unresolved", "message": str(exc)}
                    sections.append(
                        ReportSectionResult(id=section.id, title=section.title, error=error)
                    )
                    continue
                narrative = ReportNarrative(
                    page_id=page["page_id"], body=page["body"], meta=page["meta"]
                )

            sections.append(
                ReportSectionResult(
                    id=section.id, title=section.title, result=result, narrative=narrative
                )
            )

        return ReportRunResult(report_id=report.id, sections=sections)

    def describe_report(self, report_id: str, *, user: str | None = None) -> ReportStructure:
        """Return *report_id*'s section definitions — id, title, metrics, dimensions — without

        running anything (S25 follow-up): the read-only counterpart to ``run_report`` for
        discovering a section's stable ``id`` (needed by ``update_report(remove_sections=...)``)
        without paying for query execution. Same resolution scope as ``run_report`` — spans a
        committed report or a saved query, ``global/`` plus the caller's own scope.
        """
        report = self._find_report(report_id, user=user)
        sections = [
            ReportSectionInfo(
                id=section.id,
                title=section.title,
                metrics=section.query.metrics,
                dimensions=section.query.dimensions,
                narrative_from=section.narrative_from,
            )
            for section in report.sections
        ]
        return ReportStructure(report_id=report.id, title=report.title, sections=sections)

    def validate_reports(self) -> None:
        """Validate every committed report and saved query against the live semantic/knowledge layer.

        Each section's query must compile (dry-run, no execution) and each
        ``narrative_from`` must resolve to an existing knowledge-page id. Raises
        :class:`~canonic.exc.ReportError` naming the report id and section index on
        the first failure (S18) — never a silent skip. Covers both ``global/`` and
        ``user/<id>/`` scopes, including ``queries/`` — a broken reference in a saved query
        fails validation the same way a global report would.

        Compiles with :data:`~canonic.contracts.principal.SYSTEM_PRINCIPAL`: this checks
        that a report's sections compile at all, independent of any tenant — the same
        "checks compilation, not who's asking" rationale as the assertion harness
        (SPEC-E12 §7). Without it, a tenancy policy with ``on_missing_principal: deny``
        would fail every report's validation regardless of which sources it touches.
        """
        known_pages = self._known_knowledge_page_ids()
        for entry in self._load_all():
            report = entry.report
            for index, section in enumerate(report.sections):
                effective = self._effective_query(section, report, as_of=None)
                try:
                    self._query.compile_query(effective, principal=SYSTEM_PRINCIPAL)
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
