"""Assertion evaluation and the accuracy harness — the oracle for E16 (SPEC-Fuller-E15 §3)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from canonic.compiler import compile
from canonic.contracts.resolver import ContractResolver
from canonic.exc import CanonicError

if TYPE_CHECKING:
    from canonic.compiler import SemanticQuery
    from canonic.contracts.assertions import AccuracyReport, AssertionOutcome
    from canonic.contracts.models import Assertion
    from canonic.core.context import ServiceContext


class AssertionService:
    """Run assertions and the labeled accuracy/baseline harness read-only."""

    def __init__(self, ctx: ServiceContext) -> None:
        self._ctx = ctx

    async def run_assertion(
        self, assertion: Assertion, *, resolver: ContractResolver | None = None
    ) -> AssertionOutcome:
        """Compile, execute read-only, and match one assertion (SPEC-Fuller-E15 §3.2).

        Compiles the assertion's *semantic* query (so it survives compiler changes),
        executes it read-only (E2), and compares the result to ``expect`` within tolerance.
        Returns a structured :class:`~canonic.contracts.assertions.AssertionOutcome`; it never
        raises on a mismatch — callers (the CI gate, E16's harness) decide what a failure
        means. Raises :class:`~canonic.exc.ValidationFailed` only when the assertion is not in
        executable semantic-query form. ``resolver`` overrides the project's curated resolver —
        used by :meth:`run_accuracy_baseline` (SPEC-E16 Part 2 §2) to compile the same query
        against raw schema instead of canon's bindings.
        """
        from canonic.contracts.assertions import assertion_to_query, match_result

        sq = assertion_to_query(assertion)
        compiled = compile(
            sq,
            resolver or self._ctx.resolver,
            self._ctx.sources,
            connection_dialects=self._ctx.connection_dialects,
        )
        result = await self._ctx.execute(compiled.sql, self._ctx.connection_for_sql(compiled))
        return match_result(assertion, result, resolved=compiled.resolved)

    async def check_assertions(
        self, assertions: list[Assertion] | None = None
    ) -> list[AssertionOutcome]:
        """Run every executable assertion and return its outcome (SPEC-Fuller-E15 §3.4).

        Defaults to all loaded assertions (E16's accuracy harness passes the full set);
        non-executable candidate assertions are skipped. Outcomes are returned in input
        order so ``accuracy = passed / total`` is deterministic.
        """
        from canonic.contracts.assertions import is_executable

        candidates = assertions if assertions is not None else self._ctx.resolver.all_assertions()
        outcomes: list[AssertionOutcome] = []
        for assertion in candidates:
            if not is_executable(assertion):
                continue
            outcomes.append(await self.run_assertion(assertion))
        return outcomes

    async def run_accuracy_harness(
        self, assertions: list[Assertion] | None = None
    ) -> AccuracyReport:
        """Run the labeled assertion set and compute its accuracy (SPEC-Fuller-E15 §3.4).

        This is the E16 integration: every executable assertion is the oracle for one labeled
        question, so the returned :class:`~canonic.contracts.assertions.AccuracyReport` carries
        ``accuracy = passed / total``. Outcomes preserve load order, so the same assertion set
        yields the same number every run — the property that makes a regression detectable. The
        report never raises on a mismatch; the CI gate decides what a sub-target number means.
        """
        from canonic.contracts.assertions import accuracy_report

        return accuracy_report(await self.check_assertions(assertions))

    async def run_accuracy_baseline(
        self, assertions: list[Assertion] | None = None
    ) -> AccuracyReport:
        """Run the labeled assertion set against a schema-only resolver (SPEC-E16 Part 2 §2).

        Compiles and executes the same assertions as :meth:`run_accuracy_harness` but against
        :class:`~canonic.contracts.resolver.ContractResolver.schema_only` — an agent working
        from the raw physical schema, with no canonical bindings, aliases, or guardrails. A
        metric name that only resolves through a curated alias or composite binding fails to
        resolve here; that failure *is* the point — the delta between this accuracy and
        :meth:`run_accuracy_harness`'s is the measurable lift the context layer provides
        (PRD §8), not an asserted one. Deterministic and LLM-free.
        """
        from canonic.contracts.assertions import AssertionOutcome, accuracy_report, is_executable

        schema_only = ContractResolver.schema_only(self._ctx.sources)
        candidates = assertions if assertions is not None else self._ctx.resolver.all_assertions()
        outcomes: list[AssertionOutcome] = []
        for assertion in candidates:
            if not is_executable(assertion):
                continue
            try:
                outcomes.append(await self.run_assertion(assertion, resolver=schema_only))
            except CanonicError as exc:
                outcomes.append(
                    AssertionOutcome(
                        assertion_id=assertion.id,
                        passed=False,
                        detail=f"{assertion.id}: schema-only baseline could not resolve — {exc}",
                    )
                )
        return accuracy_report(outcomes)

    async def check_query_assertions(self, query: SemanticQuery, *, harness: bool) -> None:
        """Evaluate assertions matching a user query (SPEC-Fuller-E15 §3.2).

        Under ``harness`` the first failing assertion raises :class:`~canonic.exc.AssertionFailed`
        (the CI gate). In normal mode the assertions are evaluated for instrumentation only —
        any mismatch or evaluation error is swallowed so a stale assertion never blocks a
        user's query (AC2). E16's accuracy harness (#110) owns durable assertion-outcome
        persistence.
        """
        matching = self._ctx.resolver.assertions_for(query.model_dump(mode="json"))
        if not matching:
            return
        if not harness:
            import contextlib

            with contextlib.suppress(Exception):
                # Informational only — a stale assertion must never block the query (AC2).
                await self.check_assertions(matching)
            return
        from canonic.exc import AssertionFailed

        for outcome in await self.check_assertions(matching):
            if not outcome.passed:
                raise AssertionFailed(outcome.detail, assertion_id=outcome.assertion_id)
