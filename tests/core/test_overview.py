"""Unit tests for the pure overview helpers (canon/core/overview.py)."""

from __future__ import annotations

from canon.contracts.models import Example, ExampleQuery
from canon.core.overview import questions_for_group, render_question


class TestRenderQuestion:
    def test_no_dimensions_returns_total_question(self) -> None:
        q = render_question("revenue", [])
        assert q == "What is the total revenue?"

    def test_single_dimension(self) -> None:
        q = render_question("revenue", ["region"])
        assert q == "What was revenue by region?"

    def test_multiple_dimensions(self) -> None:
        q = render_question("revenue", ["region", "order_date"])
        assert q == "What was revenue by region and order date?"

    def test_underscores_replaced_with_spaces(self) -> None:
        q = render_question("order_count", ["order_date"])
        assert q == "What was order count by order date?"


class TestQuestionsForGroup:
    def _ex(self, dims: list[str], freq: int | None = None) -> Example:
        return Example(
            query=ExampleQuery(metrics=["revenue"], dimensions=dims),
            origin="observed_query",
            frequency=freq,
        )

    def test_returns_question_from_examples(self) -> None:
        metrics_with_examples = [("revenue", [self._ex(["region"])])]
        qs = questions_for_group(metrics_with_examples, ["region", "order_date"])
        assert "What was revenue by region?" in qs

    def test_highest_frequency_first(self) -> None:
        metrics_with_examples = [
            ("revenue", [self._ex(["region"], freq=5), self._ex(["order_date"], freq=20)])
        ]
        qs = questions_for_group(metrics_with_examples, ["region", "order_date"])
        assert qs[0] == "What was revenue by order date?"

    def test_deduplication(self) -> None:
        dupe = self._ex(["region"])
        metrics_with_examples = [("revenue", [dupe, dupe])]
        qs = questions_for_group(metrics_with_examples, ["region"])
        assert qs.count("What was revenue by region?") == 1

    def test_max_questions_capped(self) -> None:
        examples = [self._ex([f"dim{i}"]) for i in range(10)]
        qs = questions_for_group([("revenue", examples)], [], max_questions=3)
        assert len(qs) <= 3

    def test_fallback_when_no_examples(self) -> None:
        qs = questions_for_group([("revenue", [])], ["order_date"])
        assert len(qs) == 1
        assert "revenue" in qs[0]

    def test_fallback_never_empty_even_with_no_dims(self) -> None:
        qs = questions_for_group([("revenue", [])], [])
        assert len(qs) == 1
        assert qs[0] == "What is the total revenue?"

    def test_questions_from_multiple_metrics(self) -> None:
        metrics_with_examples = [
            ("revenue", [self._ex(["region"])]),
            ("order_count", [self._ex(["status"])]),
        ]
        qs = questions_for_group(metrics_with_examples, ["region", "status"])
        assert any("revenue" in q for q in qs)
        assert any("order count" in q for q in qs)
