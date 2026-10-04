"""Load ``pack.yaml`` manifests and discover packs in a resolved repo directory."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from pydantic import ValidationError
from ruamel.yaml import YAML

from canonic.exc import PackError
from canonic.packs.manifest import PackManifest

if TYPE_CHECKING:
    from pathlib import Path

__all__ = [
    "SkippedPack",
    "find_pack_dir",
    "list_packs",
    "list_readable_packs",
    "load_pack_manifest",
]

_PACKS_DIR = "packs"


def load_pack_manifest(pack_dir: Path) -> PackManifest:
    """Load and validate ``pack_dir/pack.yaml``, raising PackError on any problem."""
    path = pack_dir / "pack.yaml"
    if not path.exists():
        raise PackError(f"pack manifest not found: {path}")

    yaml = YAML()
    try:
        with open(path) as f:
            raw: Any = yaml.load(f) or {}
    except Exception as exc:  # noqa: BLE001 — any parse failure is a manifest error
        raise PackError(f"{path}: cannot parse YAML: {exc}") from exc

    try:
        return PackManifest.model_validate(raw)
    except ValidationError as exc:
        err = exc.errors()[0]
        loc = err["loc"]
        msg = err["msg"]
        suffix = " → ".join(str(p) for p in loc)
        message = f"{suffix}: {msg}" if suffix else msg
        raise PackError(f"{path}: {message}") from exc


@dataclass(frozen=True)
class SkippedPack:
    """A pack directory whose manifest could not be read, and why."""

    name: str
    reason: str


def _pack_dirs(repo_dir: Path) -> list[Path]:
    packs_dir = repo_dir / _PACKS_DIR
    if not packs_dir.is_dir():
        return []
    return [entry for entry in sorted(packs_dir.iterdir()) if (entry / "pack.yaml").exists()]


def list_packs(repo_dir: Path) -> list[PackManifest]:
    """Every pack manifest found directly under ``repo_dir/packs/*/pack.yaml``.

    Raises PackError on the first manifest that cannot be read, which is what a CI check
    such as ``canonic pack validate`` needs. Use :func:`list_readable_packs` to show packs
    to a user.
    """
    return [load_pack_manifest(entry) for entry in _pack_dirs(repo_dir)]


def list_readable_packs(repo_dir: Path) -> tuple[list[PackManifest], list[SkippedPack]]:
    """The manifests that can be read, and the packs that were skipped because they cannot.

    A manifest that uses a field this canonic does not know fails to parse. Listing is for
    people choosing a pack, so one such pack must not hide the others.
    """
    manifests: list[PackManifest] = []
    skipped: list[SkippedPack] = []
    for entry in _pack_dirs(repo_dir):
        try:
            manifests.append(load_pack_manifest(entry))
        except PackError as exc:
            skipped.append(SkippedPack(name=entry.name, reason=str(exc)))
    return manifests, skipped


def find_pack_dir(repo_dir: Path, name: str) -> Path:
    """The pack directory for ``name`` under ``repo_dir/packs/``, or raise PackError."""
    pack_dir = repo_dir / _PACKS_DIR / name
    if (pack_dir / "pack.yaml").exists():
        return pack_dir
    packs_dir = repo_dir / _PACKS_DIR
    available = sorted(p.name for p in packs_dir.iterdir()) if packs_dir.is_dir() else []
    raise PackError(f"pack {name!r} not found under {packs_dir}; available: {available}")
