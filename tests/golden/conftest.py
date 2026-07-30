"""Fixtures for the semantic correctness golden suite.

Copies the four zero-infrastructure example projects (jaffle-shop, dutch-railway,
saas-analytics -- each shipping a committed, read-only-opened DuckDB file -- and
rental, whose SQLite database is built here from the tracked ``setup.sql``) into
session-scoped tmp dirs. No Docker, no network, no mutation of the repo's example
files.
"""

from __future__ import annotations

import shutil
import sqlite3
from pathlib import Path

import pytest

from canonic.core.service import CanonicService

__all__ = ["EXAMPLES_ROOT", "PROJECTS"]

EXAMPLES_ROOT = Path(__file__).parents[2] / "examples"

#: The zero-infrastructure example projects this suite covers. ``ecommerce`` needs a
#: live Postgres and stays covered by tests/e2e/test_walking_skeleton.py instead.
PROJECTS = ("jaffle-shop", "dutch-railway", "saas-analytics", "rental")

# Excluding .canonic is load-bearing, not hygiene: BindingOutcomeHistory.from_project
# reads .canonic/events.jsonl into the trust tier, so a maintainer's local event log
# (verified: 125 KB on examples/rental) would make trust_score machine-dependent.
# rental.db is excluded too -- it is gitignored and rebuilt fresh from setup.sql below,
# so a maintainer's local copy (with whatever schema it happens to have) never leaks in.
_IGNORE = shutil.ignore_patterns(".canonic", "*.wal", ".DS_Store", "rental.db")


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--regen-golden",
        action="store_true",
        default=False,
        help="rewrite tests/golden/golden/**; review the diff before committing",
    )


def pytest_configure(config: pytest.Config) -> None:
    import os

    if config.getoption("--regen-golden") and os.environ.get("CI"):
        raise pytest.UsageError("--regen-golden must never run in CI")


@pytest.fixture(scope="session")
def golden_projects(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Path]:
    """Isolated copies of every zero-infra example project, built once per session."""
    projects: dict[str, Path] = {}
    for name in PROJECTS:
        dest = tmp_path_factory.mktemp(name.replace("-", "_"))
        shutil.copytree(EXAMPLES_ROOT / name, dest, dirs_exist_ok=True, ignore=_IGNORE)
        projects[name] = dest

    rental_db = projects["rental"] / "rental.db"
    setup_sql = (EXAMPLES_ROOT / "rental" / "setup.sql").read_text()
    conn = sqlite3.connect(rental_db)
    try:
        conn.executescript(setup_sql)
        conn.commit()
    finally:
        conn.close()

    return projects


@pytest.fixture(scope="session")
def golden_services(golden_projects: dict[str, Path]) -> dict[str, CanonicService]:
    """A ``CanonicService`` per project, loaded the same way the CLI/MCP do."""
    return {name: CanonicService.from_project(root) for name, root in golden_projects.items()}
