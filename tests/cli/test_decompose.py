"""``canonic decompose``: request flags, the file form, output and exit codes."""

from __future__ import annotations

import asyncio
import json
import re
from typing import TYPE_CHECKING

import pytest

from canonic.cli.app import app
from canonic.core.decompose_models import DecomposeRequest
from canonic.core.service import CanonicService
from tests.core.test_decompose import decompose_project  # noqa: F401 — fixture

if TYPE_CHECKING:
    from pathlib import Path

    from typer.testing import CliRunner

_INLINE = [
    "--metric",
    "result_rate",
    "--period-dimension",
    "sale_month",
    "--base",
    "2025-08",
    "--compare",
    "2025-09",
    "--by",
    "region,channel",
]


@pytest.fixture
def project(decompose_project: Path, monkeypatch: pytest.MonkeyPatch) -> Path:  # noqa: F811
    monkeypatch.chdir(decompose_project)
    return decompose_project


def _service_payload(project: Path, request: dict[str, object]) -> dict[str, object]:
    service = CanonicService.from_project(project)
    result = asyncio.run(service.decompose(DecomposeRequest.model_validate(request)))
    return result.model_dump(mode="json")


def test_inline_flags_json_matches_the_service(runner: CliRunner, project: Path) -> None:
    result = runner.invoke(app, ["--json", "decompose", *_INLINE])

    assert result.exit_code == 0, result.output
    assert json.loads(result.output) == _service_payload(
        project,
        {
            "metric": "result_rate",
            "period": {"dimension": "sale_month", "base": "2025-08", "compare": "2025-09"},
            "by": ["region", "channel"],
        },
    )


def test_file_form(runner: CliRunner, project: Path) -> None:
    request = {
        "metric": "price",
        "period": {"dimension": "sale_month", "base": "2025-08", "compare": "2025-09"},
        "by": ["channel"],
        "top_n": 1,
    }
    path = project / "request.json"
    path.write_text(json.dumps(request))

    result = runner.invoke(app, ["--json", "decompose", "-f", str(path)])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["method"] == "ratio_shift_share"
    assert [row["rest"] for row in payload["by"]["channel"]["rows"]] == [False, True]


def test_filters_use_the_query_grammar(runner: CliRunner, project: Path) -> None:
    result = runner.invoke(
        app, ["--json", "decompose", *_INLINE[:8], "--by", "channel", "--filter", "channel=web"]
    )

    assert result.exit_code == 0, result.output
    rows = json.loads(result.output)["by"]["channel"]["parts"]["costs"]["rows"]
    assert [row["member"] for row in rows] == ["web"]


def test_human_output_renders_every_method(runner: CliRunner, project: Path) -> None:
    for metric in ("revenue", "price", "result_rate"):
        result = runner.invoke(
            app,
            ["decompose", "--metric", metric, *_INLINE[2:]],
        )
        assert result.exit_code == 0, result.output
        assert metric in result.output
        assert "by region" in result.output


def test_file_and_flags_are_mutually_exclusive(runner: CliRunner, project: Path) -> None:
    path = project / "request.json"
    path.write_text("{}")
    result = runner.invoke(app, ["decompose", "-f", str(path), "--metric", "revenue"])
    assert result.exit_code == 2
    assert "mutually exclusive" in result.output


def test_incomplete_flags_are_a_usage_error(runner: CliRunner, project: Path) -> None:
    result = runner.invoke(app, ["decompose", "--metric", "revenue"])
    assert result.exit_code == 2
    # Rich styles option names and wraps the panel, so match a plain word without colors.
    assert "required" in re.sub(r"\x1b\[[0-9;]*m", "", result.output)


def test_a_malformed_file_is_validation_failed(runner: CliRunner, project: Path) -> None:
    path = project / "request.json"
    path.write_text(json.dumps({"metric": "revenue"}))
    result = runner.invoke(app, ["--json", "decompose", "-f", str(path)])
    assert result.exit_code == 9


def test_disabled_capability_exits_with_its_code(runner: CliRunner, project: Path) -> None:
    config = project / "canonic.yaml"
    config.write_text(config.read_text() + "decompose:\n  enabled: false\n")

    result = runner.invoke(app, ["--json", "decompose", *_INLINE])

    assert result.exit_code == 28
    assert "capability_disabled" in result.output
