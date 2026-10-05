"""An Ossie model maintained elsewhere is gated in CI like a dbt change.

Real connectors end to end: a SQLite warehouse plus an Ossie file. After bootstrap and
curation, an unchanged Ossie file passes ``canonic ingest --headless --strict``, and a
changed metric that contradicts the curated measure fails it with exit 14.
"""

from __future__ import annotations

import sqlite3
from typing import TYPE_CHECKING

import pytest
from typer.testing import CliRunner

from canonic.cli.app import app
from canonic.config import scaffold_project
from canonic.semantic.loader import dump_semantic_source, load_semantic_source
from canonic.semantic.models import Provenance

if TYPE_CHECKING:
    from pathlib import Path

_CONFIG = """\
version: 1
project:
  name: ossie-gate
  default_connection: shop_db
connections:
  - id: shop_db
    type: sqlite
    params: { path: shop.db }
  - id: shop_ossie
    type: ossie
    params: { paths: ["models/*.ossie.yaml"], target_connection: shop_db }
telemetry:
  enabled: false
"""

_MODEL = """\
version: 0.2.0.dev0
name: shop
datasets:
  - name: orders
    source: orders
    primary_key: [order_id]
metrics:
  - name: revenue
    expression:
      dialects:
        - dialect: ANSI_SQL
          expression: {expr}
"""


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    scaffold_project(tmp_path)
    (tmp_path / "canonic.yaml").write_text(_CONFIG)
    (tmp_path / "models").mkdir()
    _write_model(tmp_path, "SUM(orders.amount)")
    with sqlite3.connect(tmp_path / "shop.db") as conn:
        conn.execute("CREATE TABLE orders (order_id INTEGER PRIMARY KEY, amount NUMERIC)")
        conn.execute("INSERT INTO orders VALUES (1, 10), (2, 20)")
    monkeypatch.chdir(tmp_path)
    return tmp_path


def _write_model(root: Path, expr: str) -> None:
    (root / "models" / "shop.ossie.yaml").write_text(_MODEL.format(expr=expr))


def _ingest(*args: str) -> int:
    result = CliRunner().invoke(app, ["ingest", "--headless", "--no-pr", *args])
    return result.exit_code


def _curate(root: Path) -> None:
    path = root / "semantics" / "shop_db" / "orders.yaml"
    source = load_semantic_source(path)
    assert [m.name for m in source.measures] == ["revenue"]
    meta = source.meta.model_copy(update={"provenance": Provenance.HUMAN_CURATED})
    path.write_text(dump_semantic_source(source.model_copy(update={"meta": meta})))


def test_changed_ossie_metric_against_curated_measure_fails_strict(project: Path) -> None:
    assert _ingest("--bootstrap") == 0
    _curate(project)

    assert _ingest("--strict") == 0

    _write_model(project, "SUM(orders.amount) * 2")
    assert _ingest("--strict") == 14
