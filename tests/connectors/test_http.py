"""Tests for the shared connector HTTP timeout."""

from __future__ import annotations

import pytest

from canonic.connectors._http import CONNECT_TIMEOUT_S, READ_TIMEOUT_S, default_timeout


def test_default_timeout_is_explicit_per_phase() -> None:
    pytest.importorskip("httpx")

    timeout = default_timeout()

    assert timeout.connect == CONNECT_TIMEOUT_S
    assert timeout.read == READ_TIMEOUT_S
    assert timeout.write == READ_TIMEOUT_S
    assert timeout.pool == READ_TIMEOUT_S
