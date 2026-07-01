"""Shared fixtures: an isolated off-card app dir, a freshly built fixture card,
and an open store. The ``FOTO_UTIL_APPDIR`` override keeps all app state in a temp
dir so tests never touch the real ``~/Library/Application Support/Cull``.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from foto_util import appdir
from foto_util.store import Store
from tests import make_fixture


@pytest.fixture
def app_dir(tmp_path, monkeypatch) -> Path:
    d = tmp_path / "appdir"
    monkeypatch.setenv(appdir.ENV_OVERRIDE, str(d))
    return d


@pytest.fixture
def card(tmp_path) -> Path:
    """A synthetic a6400 card root (contains DCIM/ and management folders)."""
    return make_fixture.build(tmp_path / "card")


@pytest.fixture
def store(app_dir) -> Store:
    s = Store(appdir.db_path())
    yield s
    s.close()


def snapshot_tree(root: Path) -> dict[str, tuple[int, float, str]]:
    """Map every file under ``root`` to (size, mtime, sha256) — for proving a
    subtree is byte-stable across an operation."""
    out: dict[str, tuple[int, float, str]] = {}
    for p in sorted(root.rglob("*")):
        if p.is_file():
            st = p.stat()
            digest = hashlib.sha256(p.read_bytes()).hexdigest()
            out[str(p.relative_to(root))] = (st.st_size, st.st_mtime, digest)
    return out
