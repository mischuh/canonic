"""Load ``pack.yaml`` manifests and discover packs in a resolved repo directory."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from pydantic import ValidationError
from ruamel.yaml import YAML

from canonic.exc import PackError
from canonic.packs.manifest import PackManifest

if TYPE_CHECKING:
    from pathlib import Path

__all__ = ["find_pack_dir", "list_packs", "load_pack_manifest"]

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


def list_packs(repo_dir: Path) -> list[PackManifest]:
    """Every pack manifest found directly under ``repo_dir/packs/*/pack.yaml``."""
    packs_dir = repo_dir / _PACKS_DIR
    if not packs_dir.is_dir():
        return []
    return [
        load_pack_manifest(entry)
        for entry in sorted(packs_dir.iterdir())
        if (entry / "pack.yaml").exists()
    ]


def find_pack_dir(repo_dir: Path, name: str) -> Path:
    """The pack directory for ``name`` under ``repo_dir/packs/``, or raise PackError."""
    pack_dir = repo_dir / _PACKS_DIR / name
    if (pack_dir / "pack.yaml").exists():
        return pack_dir
    packs_dir = repo_dir / _PACKS_DIR
    available = sorted(p.name for p in packs_dir.iterdir()) if packs_dir.is_dir() else []
    raise PackError(f"pack {name!r} not found under {packs_dir}; available: {available}")
