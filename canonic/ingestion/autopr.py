"""Headless auto-PR — open a branch + PR carrying the ingest diffs and notes (SPEC-E4 §6).

The headless role (PRD §5.6 role 1): after diff emission a scheduled ingest opens an auto-PR so a
human reviews the proposed context change. Canonic **owns no write-path** (PRD non-goal) — it only
shells out to ``git`` and ``gh`` as subprocesses; the CI runner owns orchestration (scheduling /
triggering). The git/gh seam is an injected :class:`PullRequestPublisher` protocol, exactly as the
builder injects ``LLMDrafter`` and the engine injects ``AcceptedStore``, so tests substitute a fake
that records calls without touching git.

Determinism (SPEC-E4 §9): the branch name is derived from a hash of the emission's deterministic
JSON, so a re-run with identical proposals targets the same branch — no duplicate churn (§7).
"""

from __future__ import annotations

import asyncio
import hashlib
from typing import TYPE_CHECKING, Protocol

from canonic.exc import CanonicError
from canonic.ingestion.pipeline import write_emitted_diffs

if TYPE_CHECKING:
    from collections.abc import Iterable
    from pathlib import Path

    from canonic.ingestion.emitter import EmissionResult
    from canonic.ingestion.pipeline import PipelineResult

__all__ = ["AutoPRPublisher", "PullRequestPublisher", "SubprocessPublisher"]

#: Prefix for the auto-PR branch; the suffix is a content hash of the emission (§7 idempotency).
_BRANCH_PREFIX = "canonic/ingest-"


class PullRequestPublisher(Protocol):
    """The git/gh seam (SPEC-E4 §6) — injected so the auto-PR step is testable and Canonic-agnostic.

    Each method shells out to ``git`` or ``gh``; the concrete :class:`SubprocessPublisher` runs
    real subprocesses while tests inject a recording fake. Implementations raise on failure so a
    broken PR step surfaces as a structured exit, never a silent skip.
    """

    async def create_branch(self, name: str) -> None:
        """Create and switch to branch ``name`` (``git checkout -b``)."""
        ...

    async def stage(self, paths: Iterable[str]) -> None:
        """Stage ``paths`` — including deletions — for commit (``git add``)."""
        ...

    async def commit(self, message: str) -> None:
        """Commit the staged changes with ``message`` (``git commit``)."""
        ...

    async def open_pr(self, title: str, body: str) -> str:
        """Open a PR with ``title`` and ``body``; return a reference (URL/number) for comments."""
        ...

    async def comment(self, pr_ref: str, body: str) -> None:
        """Post a review comment on ``pr_ref`` (the contradiction block, §5.4)."""
        ...


class SubprocessPublisher:
    """Drives ``git`` and ``gh`` as subprocesses (SPEC-E4 §6).

    Async per the project's IO convention; every command runs through :meth:`_run`, which raises a
    :class:`CanonicError` on a non-zero exit so a failed git/gh step never passes silently. Canonic
    issues the commands but owns no write-path: ``gh`` performs the GitHub mutation.
    """

    def __init__(self, project_root: Path) -> None:
        self._root = project_root

    async def create_branch(self, name: str) -> None:
        await self._run("git", "checkout", "-b", name)

    async def stage(self, paths: Iterable[str]) -> None:
        await self._run("git", "add", "--", *paths)

    async def commit(self, message: str) -> None:
        await self._run("git", "commit", "-m", message)

    async def open_pr(self, title: str, body: str) -> str:
        out = await self._run("gh", "pr", "create", "--title", title, "--body", body)
        return out.strip()

    async def comment(self, pr_ref: str, body: str) -> None:
        await self._run("gh", "pr", "comment", pr_ref, "--body", body)

    async def _run(self, *args: str) -> str:
        """Run a subprocess in the project root, returning stdout or raising on failure."""
        proc = await asyncio.create_subprocess_exec(
            *args,
            cwd=self._root,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            detail = stderr.decode().strip() or stdout.decode().strip()
            raise CanonicError(f"auto-PR step failed: {' '.join(args)}: {detail}")
        return stdout.decode()


class AutoPRPublisher:
    """Orchestrates the headless auto-PR for one ingest run (SPEC-E4 §6).

    Materializes the emitted diffs onto a deterministically-named branch, commits them, opens a PR
    whose body is the reconciliation report, and posts contradictions as a review comment. Thin
    orchestration over an injected :class:`PullRequestPublisher`; the diff write reuses
    :func:`write_emitted_diffs` so auto-PR and the pipeline apply diffs identically.
    """

    def __init__(self, project_root: Path, publisher: PullRequestPublisher) -> None:
        self._root = project_root
        self._publisher = publisher

    async def publish(self, result: PipelineResult) -> str | None:
        """Open the auto-PR for ``result``; return its reference, or ``None`` when nothing to do.

        A run with no emitted diffs proposes no change, so no PR is opened (idempotency, §7).
        Otherwise: branch → write+stage diffs → commit → open PR (report as body) → post the
        contradiction review comment when any contradiction was flagged (§5.4).
        """
        emission = result.emission
        if not emission.diffs:
            return None

        title = self._title(emission)
        await self._publisher.create_branch(self._branch_name(emission))
        write_emitted_diffs(self._root, emission.diffs)
        await self._publisher.stage([diff.target for diff in emission.diffs])
        await self._publisher.commit(title)
        pr_ref = await self._publisher.open_pr(title, emission.render_markdown())
        if emission.notes:
            await self._publisher.comment(pr_ref, emission.render_contradictions())
        return pr_ref

    @staticmethod
    def _branch_name(emission: EmissionResult) -> str:
        """Deterministic branch name: ``canonic/ingest-<hash>`` over the emission's JSON (§7)."""
        digest = hashlib.sha256(emission.to_json().encode()).hexdigest()[:12]
        return f"{_BRANCH_PREFIX}{digest}"

    @staticmethod
    def _title(emission: EmissionResult) -> str:
        """One-line PR/commit title summarizing the run."""
        n = len(emission.diffs)
        return f"chore(canonic): ingest reconciliation — {n} diff{'s' if n != 1 else ''}"
