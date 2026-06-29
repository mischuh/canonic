"""Attach usage-backed examples to canonical metric bindings at reconciliation time (S13).

Pure collection logic (``collect_examples``) and the pipeline enrichment pass
(``ExampleEnricher``) are separated so the former is unit-testable without the pipeline.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from canon.contracts.assertions import assertion_metrics, is_executable
from canon.contracts.loader import load_assertions, load_metric_bindings
from canon.contracts.models import Example, ExampleOriginKind, ExampleQuery
from canon.ingestion.models import EvidenceKind, ReconciliationDecision, ReconciliationReport

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from canon.contracts.models import Assertion
    from canon.ingestion.models import EvidenceItem, ReconciliationEntry

__all__ = ["ExampleEnricher", "collect_examples"]

_MAX_EXAMPLES = 3


def _short_name(relation: str) -> str:
    """Return the short (unqualified) relation name, e.g. 'analytics.fct_orders' → 'fct_orders'."""
    return relation.split(".")[-1]


def collect_examples(
    metric: str,
    *,
    aliases: list[str],
    metrics_for_relation: Callable[[str], list[str]],
    assertions: list[Assertion],
    evidence: list[EvidenceItem],
) -> list[Example]:
    """Collect and rank ≤3 usage-backed examples for *metric* from evidence sources (S13).

    Sources, in priority order:
    1. ``observed_query`` evidence — coarse resolution via relation→metrics; highest frequency first.
    2. Executable assertions whose ``query.metrics`` includes *metric* (or an alias).
    3. ``usage_evidence`` — same coarse resolution; highest frequency first.

    Only real evidence is used.  Returns ``[]`` when none exists — never fabricated.
    """
    all_names: set[str] = {metric, *aliases}
    observed: list[Example] = []
    usage: list[Example] = []

    for item in evidence:
        if item.kind == EvidenceKind.OBSERVED_QUERY:
            relations: list[str] = item.payload.get("relations", [])
            for rel in relations:
                if metric in metrics_for_relation(_short_name(rel)):
                    observed.append(
                        Example(
                            query=ExampleQuery(metrics=[metric]),
                            origin=Example.make_origin(ExampleOriginKind.OBSERVED_QUERY),
                            frequency=item.payload.get("frequency") or 0,
                        )
                    )
                    break  # one example per observed-query item

        elif item.kind == EvidenceKind.USAGE_EVIDENCE:
            references: list[str] = item.payload.get("defines", {}).get("references", [])
            for ref in references:
                if metric in metrics_for_relation(_short_name(ref)):
                    usage.append(
                        Example(
                            query=ExampleQuery(metrics=[metric]),
                            origin=Example.make_origin(
                                ExampleOriginKind.USAGE_EVIDENCE,
                                item.payload.get("artifact"),
                            ),
                            frequency=item.payload.get("frequency") or 0,
                        )
                    )
                    break

    assertion_examples: list[Example] = []
    for a in assertions:
        if not is_executable(a):
            continue
        a_metrics = set(assertion_metrics(a))
        if not a_metrics.isdisjoint(all_names):
            dims: list[str] = a.query.get("dimensions", [])
            filters: list[str] = a.query.get("filters", [])
            assertion_examples.append(
                Example(
                    query=ExampleQuery(
                        metrics=list(a.query.get("metrics", [metric])),
                        dimensions=dims,
                        filters=filters,
                    ),
                    origin=Example.make_origin(ExampleOriginKind.ASSERTION, a.id),
                )
            )

    observed_sorted = sorted(observed, key=lambda e: e.frequency or 0, reverse=True)
    usage_sorted = sorted(usage, key=lambda e: e.frequency or 0, reverse=True)

    ranked = [*observed_sorted, *assertion_examples, *usage_sorted]
    return ranked[:_MAX_EXAMPLES]


class ExampleEnricher:
    """Pipeline pass that attaches examples to every canonical binding after reconciliation.

    Runs after the reconciliation engine's ``refine`` step and before diff emission.
    For each active binding it recomputes the examples list from current evidence and
    either enriches an existing report entry (ADD/EDIT) or synthesises a new EDIT entry
    when only the examples changed.  This guarantees every binding carries an up-to-date
    ``examples`` list after every reconciliation run (AC1/AC2/AC3).
    """

    def __init__(self, project_root: Path, evidence: list[EvidenceItem]) -> None:
        self._project_root = project_root
        self._evidence = evidence

    def enrich(self, report: ReconciliationReport) -> ReconciliationReport:
        """Return a new report with ``examples`` written into each binding's content."""
        from canon.ingestion.models import (
            DraftedBy,
            Proposal,
            ProposalOp,
            ReconciliationEntry,
        )
        from canon.semantic.models import Provenance

        bindings = list(load_metric_bindings(self._project_root))
        assertions = list(load_assertions(self._project_root))

        # Build source→metrics lookup from current on-disk bindings.
        source_to_metrics: dict[str, list[str]] = {}
        for b in bindings:
            if b.canonical.source:
                source_to_metrics.setdefault(b.canonical.source, []).append(b.metric)

        def metrics_for_relation(rel: str) -> list[str]:
            return source_to_metrics.get(rel, [])

        # Index existing report entries by target for fast mutation.
        entries_by_target: dict[str, ReconciliationEntry] = {e.target: e for e in report.entries}
        updated_entries: dict[str, ReconciliationEntry] = dict(entries_by_target)

        for binding in bindings:
            slug = binding.metric.replace(" ", "_").lower()
            target = f"contracts/metrics/{slug}.yaml"

            examples = collect_examples(
                binding.metric,
                aliases=list(binding.aliases),
                metrics_for_relation=metrics_for_relation,
                assertions=assertions,
                evidence=self._evidence,
            )
            examples_raw = [e.model_dump(mode="json") for e in examples]

            existing_entry = entries_by_target.get(target)

            if existing_entry is not None and existing_entry.decision in (
                ReconciliationDecision.ADD,
                ReconciliationDecision.EDIT,
            ):
                # Inject examples into the proposal content.
                new_content = dict(existing_entry.proposal.content)
                new_content["examples"] = examples_raw
                new_proposal = existing_entry.proposal.model_copy(update={"content": new_content})
                updated_entries[target] = existing_entry.model_copy(
                    update={"proposal": new_proposal}
                )
            else:
                # No binding-level entry this run — check if examples changed.
                current_raw = [e.model_dump(mode="json") for e in binding.examples]
                if examples_raw == current_raw:
                    continue  # nothing to change

                # Synthesise a minimal EDIT entry for the examples update.
                base_content: dict[str, Any] = binding.model_dump(mode="json")
                base_content["examples"] = examples_raw
                synth_proposal = Proposal(
                    target=target,
                    op=ProposalOp.EDIT,
                    content=base_content,
                    provenance=Provenance.INFERRED,
                    confidence=1.0,
                    drafted_by=DraftedBy.DETERMINISTIC,
                )
                synth_entry = ReconciliationEntry(
                    decision=ReconciliationDecision.EDIT,
                    target=target,
                    proposal=synth_proposal,
                    existing=binding.model_dump(mode="json"),
                    existing_provenance=binding.provenance,
                    auto_apply=False,
                )
                updated_entries[target] = synth_entry

        # Preserve original entry order, appending synthesised entries at the end.
        original_targets = [e.target for e in report.entries]
        seen: set[str] = set(original_targets)
        new_entries: list[ReconciliationEntry] = [
            updated_entries.get(t, entries_by_target[t]) for t in original_targets
        ]
        for t, entry in updated_entries.items():
            if t not in seen:
                new_entries.append(entry)

        return report.model_copy(update={"entries": new_entries})
