"""Self-service personal saved queries + composed reports (S19-S23, S25).

Six capabilities over ``reports/user/<own-id>/`` — ``global/`` keeps its existing PR-reviewed
workflow untouched. Every write goes through an injected :class:`~canonic.core.gitwrite.ContentWriter`
so it lands as an attributed git commit (S21 AC2) without requiring a merge/approval step (S21
AC1). Deliberately a separate collaborator from :class:`~canonic.core.reports.ReportService`: that
class is the read/execute path (``list_reports``/``run_report``), this one is the write path, with
a different dependency (the writer) and a different authorization posture (caller-scoped, not
read-only-for-everyone).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from canonic.contracts.principal import SYSTEM_PRINCIPAL, Principal
from canonic.core.models import ReportSummary, SavedQuerySummary
from canonic.exc import ReportError, ReportNotFound, TenantForbidden
from canonic.reports.ids import generate_id
from canonic.reports.loader import dump_report
from canonic.reports.loader import list_reports as load_reports
from canonic.reports.models import Report, ReportSection
from canonic.reports.scope import ReportKind, ReportScope, query_path, report_path

if TYPE_CHECKING:
    from pathlib import Path

    from canonic.compiler import SemanticQuery
    from canonic.core.context import ServiceContext
    from canonic.core.gitwrite import ContentWriter
    from canonic.core.query import QueryService
    from canonic.reports.loader import LoadedReport

__all__ = ["SavedContentService"]

_ANONYMOUS_PRINCIPAL = Principal(tenant=None)


def _require_user(user: str | None) -> str:
    """The caller's own id, or a clear failure — never a write under an anonymous scope."""
    if not user:
        raise ReportError(
            "no caller identity available; personal saved queries and reports require one "
            "(pass --user on the CLI, or an authenticated principal over MCP)"
        )
    return user


def _default_title(query: SemanticQuery) -> str:
    if not query.metrics:
        return "Saved query"
    label = query.metrics[0].replace("_", " ")
    if not query.dimensions:
        return f"Total {label}"
    dims = " and ".join(d.replace("_", " ") for d in query.dimensions)
    return f"{label} by {dims}"


def _dedupe_id(base: str, used: set[str]) -> str:
    if base not in used:
        return base
    n = 2
    while f"{base}-{n}" in used:
        n += 1
    return f"{base}-{n}"


class SavedContentService:
    """Save/list/delete queries and compose/update/delete personal reports."""

    def __init__(self, ctx: ServiceContext, query: QueryService, writer: ContentWriter) -> None:
        self._ctx = ctx
        self._query = query
        self._writer = writer

    def _require_manage_saved_content(self, principal: Principal | None) -> None:
        """Fail-closed gate on all six capabilities (SPEC-E12-style role gate).

        Mirrors :meth:`QueryService._enforce_run_sql_gate`: a role with
        ``manage_saved_content: false`` is refused outright, before any other check (missing
        identity, unknown id) runs — not every caller who can read canonic's data should be
        able to save/delete their own queries and reports.
        """
        bound = principal if principal is not None else _ANONYMOUS_PRINCIPAL
        if not self._ctx.resolver.authz_for(bound).manage_saved_content:
            raise TenantForbidden(
                "role denies managing saved queries/reports (manage_saved_content: false)"
            )

    def _root(self) -> Path:
        if self._ctx.project_root is None:
            raise ReportError("no canonic project found; cannot save or compose here")
        return self._ctx.project_root

    def _own(self, user: str, kind: ReportKind) -> list[LoadedReport]:
        return [
            r
            for r in load_reports(self._root())
            if r.scope is ReportScope.USER and r.owner == user and r.kind is kind
        ]

    def _find_own(self, item_id: str, user: str, kind: ReportKind) -> LoadedReport:
        for loaded in self._own(user, kind):
            if loaded.report.id == item_id:
                return loaded
        noun = "query" if kind is ReportKind.QUERY else "report"
        raise ReportNotFound(f"{noun} {item_id!r} matches none of {user!r}'s own {noun}s")

    # ------------------------------------------------------------------
    # Saved queries (S19, S20, S21, S23 AC2)
    # ------------------------------------------------------------------

    async def save_query(
        self,
        query: SemanticQuery,
        *,
        title: str | None = None,
        question: str | None = None,
        user: str | None = None,
        principal: Principal | None = None,
    ) -> SavedQuerySummary:
        """Validate *query* compiles, then write it as a new one-section file (S19 AC1)."""
        self._require_manage_saved_content(principal)
        owner = _require_user(user)
        self._query.compile_query(query, principal=SYSTEM_PRINCIPAL)

        resolved_title = title or _default_title(query)
        query_id = generate_id(resolved_title, seed=query.model_dump_json())
        report = Report(
            id=query_id,
            title=resolved_title,
            question=question,
            sections=[ReportSection(title=resolved_title, query=query, id=query_id)],
        )
        path = query_path(self._root(), owner, query_id)
        await self._writer.write(
            path, dump_report(report), message=f"save_query: {query_id}", author=owner
        )
        return SavedQuerySummary(
            id=query_id,
            title=resolved_title,
            question=question,
            metrics=query.metrics,
            dimensions=query.dimensions,
        )

    def list_saved_queries(
        self, *, user: str | None = None, principal: Principal | None = None
    ) -> list[SavedQuerySummary]:
        """Directory listing of the caller's own saved queries only (S20 AC1)."""
        self._require_manage_saved_content(principal)
        owner = _require_user(user)
        summaries = [
            SavedQuerySummary(
                id=loaded.report.id,
                title=loaded.report.title,
                question=loaded.report.question,
                metrics=loaded.report.sections[0].query.metrics,
                dimensions=loaded.report.sections[0].query.dimensions,
            )
            for loaded in self._own(owner, ReportKind.QUERY)
        ]
        return sorted(summaries, key=lambda s: s.id)

    async def delete_query(
        self, query_id: str, *, user: str | None = None, principal: Principal | None = None
    ) -> None:
        """Remove one of the caller's own saved queries (S21 AC3, S23 AC2)."""
        self._require_manage_saved_content(principal)
        owner = _require_user(user)
        loaded = self._find_own(query_id, owner, ReportKind.QUERY)
        await self._writer.remove(loaded.path, message=f"delete_query: {query_id}", author=owner)

    # ------------------------------------------------------------------
    # Composed reports (S22, S23, S25)
    # ------------------------------------------------------------------

    def _sections_from(
        self, query_ids: list[str], user: str, used_ids: set[str]
    ) -> list[ReportSection]:
        sections: list[ReportSection] = []
        for qid in query_ids:
            source = self._find_own(qid, user, ReportKind.QUERY)
            section_id = _dedupe_id(source.report.id, used_ids)
            used_ids.add(section_id)
            source_section = source.report.sections[0]
            sections.append(
                ReportSection(
                    title=source.report.title,
                    query=source_section.query,
                    narrative_from=source_section.narrative_from,
                    id=section_id,
                )
            )
        return sections

    async def compose_report(
        self,
        title: str,
        from_: list[str],
        *,
        user: str | None = None,
        principal: Principal | None = None,
    ) -> ReportSummary:
        """Copy N of the caller's own saved queries into a new personal report (S22 AC1)."""
        self._require_manage_saved_content(principal)
        owner = _require_user(user)
        sections = self._sections_from(from_, owner, used_ids=set())
        report_id = generate_id(title, seed=",".join(from_))
        report = Report(id=report_id, title=title, sections=sections)
        path = report_path(self._root(), owner, report_id)
        await self._writer.write(
            path, dump_report(report), message=f"compose_report: {report_id}", author=owner
        )
        return ReportSummary(id=report_id, title=title, scope=f"user:{owner}")

    async def update_report(
        self,
        report_id: str,
        *,
        add_from: list[str] | None = None,
        remove_sections: list[str] | None = None,
        user: str | None = None,
        principal: Principal | None = None,
    ) -> ReportSummary:
        """Append and/or remove sections on one of the caller's own reports in place (S25)."""
        self._require_manage_saved_content(principal)
        owner = _require_user(user)
        loaded = self._find_own(report_id, owner, ReportKind.REPORT)
        existing = loaded.report

        remove = set(remove_sections or [])
        kept = [s for s in existing.sections if s.id not in remove]
        used_ids = {s.id for s in kept if s.id is not None}
        added = self._sections_from(add_from or [], owner, used_ids)
        sections = kept + added

        if not sections:
            raise ReportError(
                f"update_report {report_id!r}: removing every section would leave an empty "
                "report, which is not a valid report file"
            )

        updated = existing.model_copy(update={"sections": sections})
        await self._writer.write(
            loaded.path, dump_report(updated), message=f"update_report: {report_id}", author=owner
        )
        return ReportSummary(id=report_id, title=existing.title, scope=f"user:{owner}")

    async def delete_report(
        self, report_id: str, *, user: str | None = None, principal: Principal | None = None
    ) -> None:
        """Remove one of the caller's own personal reports, never a query (S23 AC1)."""
        self._require_manage_saved_content(principal)
        owner = _require_user(user)
        loaded = self._find_own(report_id, owner, ReportKind.REPORT)
        await self._writer.remove(loaded.path, message=f"delete_report: {report_id}", author=owner)
