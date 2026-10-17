"""The readmes and guides of every example still describe the project as it is.

``test_examples_consistency`` loads the projects, but nothing read the prose next to them. A
quickstart query that does not compile and a metric count that no longer matches the
directory are exactly what drifted before, and a reader hits them first. This compiles every
``canonic query`` line of an example's readme and guide, with the same flag handling the CLI
uses, and compares the metric-contract counts they state with the contract files. It
compiles only, so it needs no database and no network.

Lines that start with ``$`` are output transcripts that may fail on purpose (a query without
a tenant, a denied metric), so they are not checked.
"""

from __future__ import annotations

import argparse
import re
import shlex
from pathlib import Path

import pytest

from canonic.cli._tenant import cli_tenant_principal
from canonic.cli.commands import build_semantic_query
from canonic.core.service import CanonicService

_REPO = Path(__file__).parents[2]
_EXAMPLES = _REPO / "examples"
_GUIDES = _REPO / "docs" / "guides"

_COMMENT = re.compile(r"\s+#.*$")
_METRIC_COUNT = re.compile(r"\*{0,2}(\d+)\*{0,2} metric contracts")

_QUERY_FLAGS = argparse.ArgumentParser(add_help=False, exit_on_error=False)
for _flag in ("--metrics", "--dimensions", "--filter", "--via", "--tenant"):
    _QUERY_FLAGS.add_argument(_flag, action="append")
_QUERY_FLAGS.add_argument("--limit", type=int)


def _docs(project: str) -> list[Path]:
    candidates = [_EXAMPLES / project / "README.md", _GUIDES / f"{project}.mdx"]
    return [path for path in candidates if path.is_file()]


def _query_lines(path: Path) -> list[str]:
    return [
        _COMMENT.sub("", line.strip())
        for line in path.read_text().splitlines()
        if line.strip().startswith("canonic query ")
    ]


_PROJECTS = sorted(p.name for p in _EXAMPLES.iterdir() if (p / "canonic.yaml").is_file())
_QUERIES = [
    pytest.param(project, doc, line, id=f"{project}-{doc.name}-{i}")
    for project in _PROJECTS
    for doc in _docs(project)
    for i, line in enumerate(_query_lines(doc))
]


def test_quickstart_queries_are_discovered() -> None:
    assert len(_QUERIES) >= 10


@pytest.mark.parametrize(("project", "doc", "line"), _QUERIES)
def test_documented_query_compiles(project: str, doc: Path, line: str) -> None:
    args, unknown = _QUERY_FLAGS.parse_known_args(shlex.split(line)[2:])
    assert not unknown, f"{doc.name}: unexpected arguments {unknown}"

    query = build_semantic_query(
        None, args.metrics, args.dimensions, args.filter, args.via, args.limit
    )
    service = CanonicService.from_project(_EXAMPLES / project)

    service.compile_query(query, principal=cli_tenant_principal(args.tenant))


@pytest.mark.parametrize("project", _PROJECTS)
def test_stated_metric_contract_counts_match_the_directory(project: str) -> None:
    actual = len(list((_EXAMPLES / project / "contracts" / "metrics").glob("*.yaml")))
    for doc in _docs(project):
        for stated in _METRIC_COUNT.findall(doc.read_text()):
            assert int(stated) == actual, f"{doc.name} states {stated}, found {actual}"
