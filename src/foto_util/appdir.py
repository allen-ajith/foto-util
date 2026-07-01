"""Resolution of the off-card application directory (guard G6).

Every piece of app state — the SQLite database and the trash — lives under a
single per-user directory that is *never* on the card. On macOS that is
``~/Library/Application Support/foto-util/``. The location can be overridden with the
``FOTO_UTIL_APPDIR`` environment variable, which the test-suite uses to keep its
state in a temp dir.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

APP_NAME = "foto-util"
ENV_OVERRIDE = "FOTO_UTIL_APPDIR"


def _default_base() -> Path:
    """Platform-appropriate base directory. macOS first, OS-agnostic fallback."""
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / APP_NAME
    # XDG-style fallback for Linux and friends; keeps the core OS-agnostic.
    xdg = os.environ.get("XDG_DATA_HOME")
    base = Path(xdg) if xdg else Path.home() / ".local" / "share"
    return base / APP_NAME


def app_dir() -> Path:
    """Return (creating if needed) the root app directory."""
    override = os.environ.get(ENV_OVERRIDE)
    base = Path(override).expanduser() if override else _default_base()
    base.mkdir(parents=True, exist_ok=True)
    return base


def _subdir(name: str) -> Path:
    p = app_dir() / name
    p.mkdir(parents=True, exist_ok=True)
    return p


def db_path() -> Path:
    """Absolute path to the SQLite store (not created here)."""
    return app_dir() / "foto-util.db"


def trash_root() -> Path:
    """Root of the off-card trash. Per-volume subdirs live underneath."""
    return _subdir("trash")
