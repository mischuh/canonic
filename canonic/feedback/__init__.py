"""E11 feedback loop — turns real-world answer outcomes into E4 evidence and an E14 trust signal.

E16 records ``answer_outcome`` events (SPEC-E16 Part 2); this package acts on them (SPEC-E11).
Attribution routing is enforced once, at aggregation (:class:`BindingOutcomeHistory`): only
``wrong_definition`` outcomes ever touch a binding's pattern gate or trust cap.
"""

from canonic.feedback.evidence import outcome_evidence
from canonic.feedback.history import BindingOutcomeHistory, OutcomeRecord
from canonic.feedback.report import BindingFeedbackEntry, FeedbackReport, build_feedback_report

__all__ = [
    "BindingFeedbackEntry",
    "BindingOutcomeHistory",
    "FeedbackReport",
    "OutcomeRecord",
    "build_feedback_report",
    "outcome_evidence",
]
