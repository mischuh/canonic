"""Resolve a pack repo — a local directory or a git URL — into a local directory (§2.5).

Packs are git repositories, matching E1's "everything is files, reviewed like code"
posture: no new service, no registry. A local path is used as-is (the cheapest path for
the air-gapped case the amendment leaves open, §8); a git URL is shallow-cloned once and
re-fetched on every subsequent call so ``canonic pack add``/``pack list`` always see the
repo's current default branch.
"""

from __future__ import annotations

import hashlib
import logging
import subprocess
from pathlib import Path

from canonic.exc import PackError

__all__ = ["resolve_repo"]

logger = logging.getLogger(__name__)

_CACHE_DIR = ".canonic/packs-cache"
_GIT_TIMEOUT_S = 60


def _is_local_path(repo: str) -> bool:
    return not (
        repo.startswith("http://")
        or repo.startswith("https://")
        or repo.startswith("git@")
        or repo.startswith("ssh://")
    )


def resolve_repo(repo: str, *, project_root: Path) -> Path:
    """Return a local directory for ``repo``.

    A local path is returned as-is (must already exist). A git URL is cached at
    ``.canonic/packs-cache/<hash>`` — cloned on first use, fetched+reset on every later
    call — and that cache directory is returned.
    """
    if _is_local_path(repo):
        local = Path(repo).expanduser()
        if not local.is_dir():
            raise PackError(f"pack repo path not found: {local}")
        return local

    cache_key = hashlib.sha1(repo.encode(), usedforsecurity=False).hexdigest()[:16]
    dest = project_root / _CACHE_DIR / cache_key
    if dest.is_dir():
        _run_git(["fetch", "--depth", "1", "origin"], cwd=dest)
        _run_git(["reset", "--hard", "origin/HEAD"], cwd=dest)
    else:
        dest.parent.mkdir(parents=True, exist_ok=True)
        _run_git(["clone", "--depth", "1", repo, str(dest)], cwd=project_root)
    return dest


def _run_git(args: list[str], *, cwd: Path) -> None:
    try:
        subprocess.run(
            ["git", *args],
            cwd=cwd,
            check=True,
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT_S,
        )
    except FileNotFoundError as exc:
        raise PackError("git is required to fetch a pack repo but was not found on PATH") from exc
    except subprocess.CalledProcessError as exc:
        raise PackError(f"git {' '.join(args)} failed: {exc.stderr.strip()}") from exc
    except subprocess.TimeoutExpired as exc:
        raise PackError(f"git {' '.join(args)} timed out after {_GIT_TIMEOUT_S}s") from exc
