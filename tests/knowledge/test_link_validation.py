"""``canonic validate`` rejects links between metrics and pages that point at nothing."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from canonic.exc import ContractError
from canonic.knowledge.link_validation import validate_knowledge_links

if TYPE_CHECKING:
    from pathlib import Path


def _write(root: Path, relative: str, text: str) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def _binding(root: Path, extra: str = "") -> None:
    _write(
        root,
        "contracts/metrics/revenue.yaml",
        "metric: revenue\ncanonical:\n  source: orders\n  measure: total_revenue\n"
        f"aliases: [rev]\nstatus: active\n{extra}",
    )


def _page(root: Path, page_id: str, front: str = "", scope: str = "global") -> None:
    location = (
        f"knowledge/{scope}/{page_id}.md"
        if scope == "global"
        else f"knowledge/user/me/{page_id}.md"
    )
    _write(root, location, f"---\n{front}---\nBody.\n")


def test_a_project_without_links_is_valid(tmp_path: Path) -> None:
    _binding(tmp_path)

    validate_knowledge_links(tmp_path)


def test_valid_links_pass(tmp_path: Path) -> None:
    _binding(tmp_path, "knowledge_refs: [revenue-def]\n")
    _page(tmp_path, "revenue-def", "metrics: [revenue]\n")

    validate_knowledge_links(tmp_path)


def test_a_declared_page_that_does_not_exist_is_rejected(tmp_path: Path) -> None:
    _binding(tmp_path, "knowledge_refs: [missing]\n")

    with pytest.raises(ContractError, match=r"revenue.*'missing'"):
        validate_knowledge_links(tmp_path)


def test_a_declared_personal_page_is_rejected(tmp_path: Path) -> None:
    _binding(tmp_path, "knowledge_refs: [mine]\n")
    _page(tmp_path, "mine", scope="user")

    with pytest.raises(ContractError, match="not a global knowledge page"):
        validate_knowledge_links(tmp_path)


def test_a_page_naming_an_unknown_metric_is_rejected(tmp_path: Path) -> None:
    _binding(tmp_path)
    _page(tmp_path, "p", "metrics: [margin]\n")

    with pytest.raises(ContractError, match=r"p\.md.*'margin'"):
        validate_knowledge_links(tmp_path)


def test_a_page_naming_an_alias_is_rejected_with_the_canonical_name(tmp_path: Path) -> None:
    _binding(tmp_path)
    _page(tmp_path, "p", "metrics: [rev]\n")

    with pytest.raises(ContractError, match="canonical name 'revenue'"):
        validate_knowledge_links(tmp_path)
