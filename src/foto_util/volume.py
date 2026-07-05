"""Volume / card identification (read-only).

Two jobs:

* derive a *stable* id for the source volume, so pruning can be scoped to one
  card and resume state for other cards is never disturbed (the content hash is
  the per-shot identity; the volume id only scopes housekeeping);
* locate the card root (the folder that contains ``DCIM`` and the management
  folders) given whatever path the user pointed us at.

Nothing here writes to the card (guard G7).
"""

from __future__ import annotations

import hashlib
import os
import plistlib
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Source:
    """A candidate place to cull from, for the startup picker."""

    path: Path          # the folder to point the app at (the card root)
    name: str           # display label (volume name)
    is_card: bool       # has a DCIM/ — looks like a camera card
    dcim: Path | None   # the DCIM dir if present (what we actually scan)


def _mounted_volume_dirs() -> list[Path]:
    """Mounted volumes to offer as sources. macOS lists them under /Volumes;
    other platforms fall back to common removable-media roots."""
    roots = ["/Volumes"] if sys.platform == "darwin" else ["/media", "/run/media", "/mnt"]
    out: list[Path] = []
    for r in roots:
        base = Path(r)
        if not base.is_dir():
            continue
        for entry in sorted(base.iterdir()):
            try:
                if entry.is_dir() and not entry.is_symlink():
                    out.append(entry)
            except OSError:
                continue
    return out


def list_sources() -> list[Source]:
    """Discover cull sources: every mounted volume, cards first.

    Read-only — it only stats directories. A volume is flagged ``is_card`` when
    it contains a ``DCIM`` folder; those sort to the top of the list.
    """
    sources: list[Source] = []
    for vol in _mounted_volume_dirs():
        dcim = vol / "DCIM"
        has = dcim.is_dir()
        sources.append(Source(path=vol, name=vol.name, is_card=has,
                              dcim=dcim if has else None))
    # cards first, then alphabetical
    sources.sort(key=lambda s: (not s.is_card, s.name.lower()))
    return sources


def mount_point(path: str | Path) -> Path:
    """The mount point containing ``path`` (highest ancestor on the same fs)."""
    p = Path(path).resolve()
    try:
        dev = os.stat(p).st_dev
    except FileNotFoundError:
        p = p.parent
        dev = os.stat(p).st_dev
    while p != p.parent:
        try:
            if os.stat(p.parent).st_dev != dev:
                return p
        except OSError:
            return p
        p = p.parent
    return p


def card_root_for(scan_path: str | Path) -> Path:
    """The volume/card root for a scan target.

    If pointed at ``.../DCIM`` the root is its parent; if pointed at a folder
    that *contains* ``DCIM`` that folder is the root; otherwise (an arbitrary
    folder) the folder itself is treated as the root.
    """
    p = Path(scan_path).resolve()
    if p.name.upper() == "DCIM":
        return p.parent
    if (p / "DCIM").is_dir():
        return p
    return p


def is_card(root: str | Path) -> bool:
    """A root looks like a camera card if it has a ``DCIM`` directory."""
    return (Path(root) / "DCIM").is_dir()


def _macos_volume_uuid(mp: Path) -> str | None:
    if sys.platform != "darwin":
        return None
    try:
        out = subprocess.run(
            ["diskutil", "info", "-plist", str(mp)],
            capture_output=True,
            timeout=5,
            check=True,
        ).stdout
        info = plistlib.loads(out)
    except Exception:
        return None
    uuid = info.get("VolumeUUID") or info.get("DiskUUID")
    return str(uuid) if uuid else None


def volume_id_for(path: str | Path) -> str:
    """A stable id for the volume containing ``path``.

    Prefers the filesystem volume UUID (stable across remounts). Falls back to a
    device + name signature, which is good enough for scoping housekeeping; the
    per-shot content hash is the real identity that survives reformat/remount.
    """
    mp = mount_point(path)
    uuid = _macos_volume_uuid(mp)
    if uuid:
        return f"uuid:{uuid}"
    try:
        dev = os.stat(mp).st_dev
    except OSError:
        dev = 0
    return f"dev:{dev}:{mp.name or 'root'}"


def source_id_for(root: str | Path) -> str:
    """A stable id for a cull *source*.

    A card (a root with ``DCIM``) is identified by its volume, via
    :func:`volume_id_for` — stable across remounts. A plain folder is identified
    by its own resolved path: folders must never share the volume's id, or two
    folders on the same disk would pool their rows (and "Forget this card" on
    one would delete the other's state). The path digest keeps the id unique
    even after :func:`trash_dirname` sanitises it.
    """
    root = Path(root).resolve()
    if is_card(root):
        return volume_id_for(root)
    digest = hashlib.sha1(str(root).encode("utf-8")).hexdigest()[:12]
    return f"path:{digest}:{root.name or 'root'}"


def trash_dirname(volume_id: str) -> str:
    """A filesystem-safe per-volume subdirectory name for the off-card trash."""
    return re.sub(r"[^A-Za-z0-9._-]", "_", volume_id)
