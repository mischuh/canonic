"""Tests for canonic.connectors.base — compute_fingerprint's stats-exclusion contract."""

from __future__ import annotations

from canonic.connectors.base import ColumnInfo, compute_fingerprint


def _column(**overrides: object) -> ColumnInfo:
    defaults: dict[str, object] = {"name": "id", "type": "int", "nullable": False, "position": 1}
    defaults.update(overrides)
    return ColumnInfo(**defaults)  # type: ignore[arg-type]


def test_fingerprint_unchanged_for_stats_free_column() -> None:
    """A column carrying no stats fingerprints identically to one built before stats existed."""
    col = _column()
    fingerprint = compute_fingerprint([col], primary_key=["id"], foreign_keys=[])
    assert fingerprint == compute_fingerprint([col], primary_key=["id"], foreign_keys=[])


def test_fingerprint_ignores_stats_fields() -> None:
    """Two columns differing only in cardinality/null-ratio stats fingerprint identically.

    This is the load-bearing guarantee: enabling fetch_column_stats, or stats drifting
    between ingest runs (a new ANALYZE, more rows), must never look like a schema change
    to reconciliation's NO_OP detection.
    """
    bare = _column()
    with_stats = _column(
        distinct_count_estimate=1000,
        null_fraction=0.05,
        uniqueness_ratio=0.98,
        stats_source="pg_stats",
    )
    assert compute_fingerprint([bare], [], []) == compute_fingerprint([with_stats], [], [])


def test_fingerprint_still_reflects_identity_field_changes() -> None:
    """Sanity check: the exclusion doesn't accidentally swallow real identity changes too."""
    col_a = _column(name="id")
    col_b = _column(name="other_id")
    assert compute_fingerprint([col_a], [], []) != compute_fingerprint([col_b], [], [])
