#!/usr/bin/env python3
"""CI gate: fail a PR that bumps CONTRACT_SCHEMA without a CONTRACT_CHANGELOG.md entry.

See docs/SPEC-P0-interface-freeze.md §7 — contract_schema changes must be a
deliberate, reviewed event with an ADR/PR classification, never a side
effect of an unrelated code change.
"""

from __future__ import annotations

import subprocess
import sys

CONTRACT_FILE = "canonic/contract.py"
CHANGELOG_FILE = "CONTRACT_CHANGELOG.md"


def changed_files(base_ref: str) -> list[str]:
    """Return paths changed between base_ref and the current HEAD."""
    result = subprocess.run(
        ["git", "diff", "--name-only", f"{base_ref}...HEAD"],
        check=True,
        capture_output=True,
        text=True,
    )
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def contract_schema_value_changed(base_ref: str) -> bool:
    """Return True if the CONTRACT_SCHEMA assignment's value differs from base_ref."""
    result = subprocess.run(
        ["git", "diff", f"{base_ref}...HEAD", "--", CONTRACT_FILE],
        check=True,
        capture_output=True,
        text=True,
    )
    added_or_removed = (
        line
        for line in result.stdout.splitlines()
        if line.startswith(("+", "-")) and not line.startswith(("+++", "---"))
    )
    return any("CONTRACT_SCHEMA" in line for line in added_or_removed)


def main() -> int:
    if len(sys.argv) != 2:
        print(f"usage: {sys.argv[0]} <base-ref>", file=sys.stderr)
        return 2

    base_ref = sys.argv[1]
    files = changed_files(base_ref)

    if CONTRACT_FILE not in files:
        print(f"{CONTRACT_FILE} unchanged — nothing to check.")
        return 0

    if not contract_schema_value_changed(base_ref):
        print(f"{CONTRACT_FILE} changed but CONTRACT_SCHEMA value is unchanged — OK.")
        return 0

    if CHANGELOG_FILE in files:
        print(f"CONTRACT_SCHEMA changed and {CHANGELOG_FILE} was updated — OK.")
        return 0

    print(
        f"::error::CONTRACT_SCHEMA changed in {CONTRACT_FILE} but {CHANGELOG_FILE} "
        "was not updated in this PR. Per docs/SPEC-P0-interface-freeze.md §7, every "
        "contract_schema change needs an ADR/PR classification (MINOR or MAJOR) "
        f"recorded as a new entry in {CHANGELOG_FILE}.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
