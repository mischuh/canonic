"""Tests for the one-time `{{param}}` substitution (§2.3)."""

from __future__ import annotations

import pytest

from canonic.exc import PackError
from canonic.packs.templating import substitute


def test_substitute_replaces_every_token():
    text = "name: {{a}}\ntable: {{schema}}.{{table}}\n"
    out = substitute(text, {"a": "foo", "schema": "public", "table": "events"}, source="x.yaml")
    assert out == "name: foo\ntable: public.events\n"


def test_substitute_tolerates_whitespace_inside_braces():
    out = substitute("{{ a }}", {"a": "x"}, source="x.yaml")
    assert out == "x"


def test_substitute_no_tokens_is_passthrough():
    assert substitute("plain text, no tokens", {}, source="x.yaml") == "plain text, no tokens"


def test_substitute_raises_on_unresolved_token():
    with pytest.raises(PackError, match=r"x\.yaml.*\{\{missing\}\}"):
        substitute("{{missing}}", {}, source="x.yaml")
