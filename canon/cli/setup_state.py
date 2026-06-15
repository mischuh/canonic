"""Checkpoint state for the ``canon setup`` wizard (SPEC E1 §4 resumability).

Progress is persisted to ``.canon/setup-state.json`` after each completed step so
an interrupted run resumes from the next step instead of restarting. The recorded
connection is written only after its ``test_connection()`` passes, so a resumed
run never re-derives a broken connection.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import BaseModel, ValidationError

from canon.config import LOCAL_STATE_DIR, Connection, LLMConfig

if TYPE_CHECKING:
    from pathlib import Path

_STATE_FILE = "setup-state.json"

# Canonical step identifiers, in wizard order. ``completed_steps`` holds a subset.
STEP_NAME = "name"
STEP_CONNECTION = "connection"
STEP_LLM = "llm"
STEP_SCHEMA = "schema"


class SetupState(BaseModel):
    """Resumable checkpoint of an in-progress setup wizard run."""

    project_name: str | None = None
    connection: Connection | None = None
    llm: LLMConfig | None = None
    schema_previewed: bool = False
    completed_steps: list[str] = []

    def mark(self, step: str) -> None:
        """Record step as completed (idempotent)."""
        if step not in self.completed_steps:
            self.completed_steps.append(step)

    def done(self, step: str) -> bool:
        """Return True if step was already completed in a prior run."""
        return step in self.completed_steps


def _state_path(root: Path) -> Path:
    return root / LOCAL_STATE_DIR / _STATE_FILE


def load_state(root: Path) -> SetupState | None:
    """Return the saved checkpoint under root, or None when absent/unreadable."""
    path = _state_path(root)
    if not path.exists():
        return None
    try:
        return SetupState.model_validate_json(path.read_text())
    except (ValidationError, ValueError, OSError):
        # A corrupt checkpoint should not wedge setup — fall back to a fresh run.
        return None


def save_state(root: Path, state: SetupState) -> None:
    """Persist state to ``.canon/setup-state.json``, creating ``.canon/`` if needed."""
    local = root / LOCAL_STATE_DIR
    local.mkdir(parents=True, exist_ok=True)
    local.chmod(0o700)
    _state_path(root).write_text(state.model_dump_json(indent=2))


def clear_state(root: Path) -> None:
    """Remove the checkpoint after a successful run (idempotent)."""
    _state_path(root).unlink(missing_ok=True)
