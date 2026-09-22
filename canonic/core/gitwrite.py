"""The git-commit-on-write seam for self-service personal writes (S21 AC2).

``global/`` reports go through the standard PR-reviewed workflow (unchanged). Everything under
``reports/user/<id>/`` is self-service — no merge/approval step — but every write still becomes a
git commit attributed to the calling principal, so the audit trail survives the removed review
gate (AMENDMENT-user-scoped-queries-reports §2).

Mirrors the injected-seam shape of ``canonic/ingestion/autopr.py``
(``PullRequestPublisher``/``SubprocessPublisher``): a ``Protocol`` so tests substitute a recording
fake, and a concrete implementation that shells out to ``git`` as a subprocess, raising on
failure so a broken write never passes silently.
"""

from __future__ import annotations

import asyncio
import os
from typing import TYPE_CHECKING, Protocol

from canonic.exc import CanonicError

if TYPE_CHECKING:
    from pathlib import Path

__all__ = ["ContentWriter", "FilesystemContentWriter", "GitContentWriter"]


class ContentWriter(Protocol):
    """Write/remove one file and (when possible) commit the change, attributed to *author*."""

    async def write(self, path: Path, content: str, *, message: str, author: str) -> None: ...

    async def remove(self, path: Path, *, message: str, author: str) -> None: ...


class FilesystemContentWriter:
    """Plain filesystem write/unlink, no git — used outside a git work tree.

    A project living in a throwaway tmp dir (tests, ``examples/`` fixtures run standalone) has no
    repository to commit into; degrading to a filesystem-only write keeps ``save_query`` and
    friends usable there without pretending a commit happened.
    """

    async def write(self, path: Path, content: str, *, message: str, author: str) -> None:
        del message, author  # nothing to attribute without a repo
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)

    async def remove(self, path: Path, *, message: str, author: str) -> None:
        del message, author
        path.unlink()


class GitContentWriter:
    """Write/unlink + ``git add`` + ``git commit --author`` in *project_root* (S21 AC2).

    Whether ``project_root`` is a git work tree is probed once (``git rev-parse
    --is-inside-work-tree``) and memoized; when it is not, writes silently degrade to
    :class:`FilesystemContentWriter` behavior rather than failing every save/delete outside a
    repo.
    """

    def __init__(self, project_root: Path) -> None:
        self._root = project_root
        self._is_repo: bool | None = None

    async def write(self, path: Path, content: str, *, message: str, author: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        if await self._in_repo():
            rel = str(path.relative_to(self._root))
            await self._run("git", "add", "--", rel)
            await self._commit(message, author)

    async def remove(self, path: Path, *, message: str, author: str) -> None:
        was_tracked = await self._in_repo()
        path.unlink()
        if was_tracked:
            rel = str(path.relative_to(self._root))
            await self._run("git", "add", "--", rel)
            await self._commit(message, author)

    async def _commit(self, message: str, author: str) -> None:
        # Author/committer identity is passed via env, not global git config (never mutated by
        # canonic) or a bare --author flag (git still requires a committer identity even then).
        email = f"{author}@canonic.invalid"
        await self._run(
            "git",
            "commit",
            "-m",
            message,
            env={
                "GIT_AUTHOR_NAME": author,
                "GIT_AUTHOR_EMAIL": email,
                "GIT_COMMITTER_NAME": author,
                "GIT_COMMITTER_EMAIL": email,
            },
        )

    async def _in_repo(self) -> bool:
        if self._is_repo is None:
            try:
                await self._run("git", "rev-parse", "--is-inside-work-tree")
                self._is_repo = True
            except CanonicError:
                self._is_repo = False
        return self._is_repo

    async def _run(self, *args: str, env: dict[str, str] | None = None) -> str:
        full_env = {**os.environ, **env} if env is not None else None
        proc = await asyncio.create_subprocess_exec(
            *args,
            cwd=self._root,
            env=full_env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            detail = stderr.decode().strip() or stdout.decode().strip()
            raise CanonicError(f"git step failed: {' '.join(args)}: {detail}")
        return stdout.decode()
