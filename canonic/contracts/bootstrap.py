"""Deterministic contract generation from semantic source schema (OB-S1 fast path).

Writes one ``contracts/metrics/<slug>.yaml`` per p0-compilable measure discovered
in the project's semantic sources.  Used both by the setup wizard (golden path) and
by ``canonic mcp start`` (auto-heal when contracts are missing).
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING

from ruamel.yaml import YAML

from canonic.semantic.models import NormalizedType

if TYPE_CHECKING:
    from pathlib import Path

    from canonic.semantic.models import SemanticSource

__all__ = ["infer_p0_pairs", "write_inferred_contracts"]

logger = logging.getLogger(__name__)

_ID_RE = re.compile(r"(^id$|_(id|fk|key)$)", re.IGNORECASE)
_SUMMABLE = {NormalizedType.INT, NormalizedType.FLOAT, NormalizedType.DECIMAL}


def _contract_source(path: Path) -> str | None:
    """Best-effort read of an existing contract's ``canonical.source``; None if unreadable."""
    yaml = YAML()
    try:
        with open(path) as f:
            raw = yaml.load(f) or {}
    except Exception:  # noqa: BLE001 — a malformed existing file must not abort bootstrap
        return None
    canonical = raw.get("canonical") if isinstance(raw, dict) else None
    return canonical.get("source") if isinstance(canonical, dict) else None


def _slugify(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


def infer_p0_pairs(source: SemanticSource) -> list[tuple[str, str]]:
    """Return ``(name, expr)`` pairs for every measure inferable from column types.

    Always emits ``("row_count", "count(*)")``.  For each numeric column that is
    not a surrogate key (plain ``id`` or ``*_id``/``*_fk``/``*_key`` suffix) it
    also emits ``(f"total_{col.name}", f"sum({col.name})")``.

    These are all p0-compilable (additive aggregates on simple columns).
    """
    pairs: list[tuple[str, str]] = [("row_count", "count(*)")]
    for col in source.columns:
        if col.type in _SUMMABLE and not _ID_RE.search(col.name):
            pairs.append((f"total_{col.name}", f"sum({col.name})"))
    return pairs


def write_inferred_contracts(root: Path, sources: list[SemanticSource]) -> int:
    """Write one ``contracts/metrics/<slug>.yaml`` per p0-compilable measure.

    When a source already carries measures (bootstrapped with current builder code),
    those are used directly.  When ``source.measures`` is empty (YAML written by an
    older builder that did not yet generate measures), the function falls back to
    ``infer_p0_pairs`` so contracts are always produced.

    Skips files that already exist for the *same* source (or whose ``canonical.source``
    can't be determined, e.g. a human replaced the contents entirely), so human edits
    are never overwritten and re-running is idempotent. When a same-named measure from
    a *different*, identifiable source would otherwise clobber (or be silently dropped
    by) an existing contract, the new one is written under a source-qualified name
    instead (e.g. ``orders_row_count``) so both stay queryable.

    Returns the number of new files written.
    """
    contracts_dir = root / "contracts" / "metrics"
    contracts_dir.mkdir(parents=True, exist_ok=True)
    written = 0
    for source in sources:
        pairs = (
            [(m.name, m.name) for m in source.measures if m.is_p0_compilable]
            if source.measures
            else infer_p0_pairs(source)
        )
        for name, _ in pairs:
            slug = _slugify(name)
            path = contracts_dir / f"{slug}.yaml"
            metric_name = name
            if path.exists():
                existing_source = _contract_source(path)
                if existing_source is None or existing_source == source.name:
                    continue  # same source re-run, or human-edited file — idempotent no-op
                metric_name = f"{source.name}_{name}"
                slug = _slugify(metric_name)
                path = contracts_dir / f"{slug}.yaml"
                if path.exists():
                    continue  # already qualified+written for this exact source+measure
                logger.warning(
                    "contract name %r collides with an existing contract from another "
                    "source; wrote %r instead",
                    name,
                    metric_name,
                )
            path.write_text(
                f"metric: {metric_name}\n"
                f"canonical:\n"
                f"  source: {source.name}\n"
                f"  measure: {name}\n"
                f"provenance: human_curated\n"
                f"status: active\n"
            )
            written += 1
    return written
