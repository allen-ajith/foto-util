"""Scan orchestration (read-only): enumerate → pair → hash → time → group →
upsert. Designed to run on a background thread while the UI reads rows
progressively; here it is a plain function plus a tiny progress callback.

Already-decided shots keep their decision and trash pointers across a
rescan (resume) — :meth:`foto_util.store.Store.upsert_scanned` only refreshes the
volatile path/volume/group fields.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Callable

from . import hashing, meta, pairing, volume
from .grouping import GroupItem, SCENE_GAP_S, assign_groups
from .store import Store

ProgressFn = Callable[[int, int], None]  # (done, total)

_TRAILING_NUM = re.compile(r"(\d+)\D*$")


def _file_number(stem: str) -> int:
    """Trailing digits of a stem (``DSC00123`` → 123) for the tiebreaker."""
    m = _TRAILING_NUM.search(stem)
    return int(m.group(1)) if m else 0


def scan(
    root: str | Path,
    store: Store,
    volume_id: str | None,
    *,
    gap_s: float = SCENE_GAP_S,
    progress: ProgressFn | None = None,
    should_stop: "Callable[[], bool] | None" = None,
) -> int:
    """Index ``root`` into ``store``. Returns the number of shots processed.

    Strictly read-only with respect to the card (guard G7): it lists, stats,
    hashes, and reads EXIF, but never writes there.

    ``should_stop`` is polled each shot; when it returns True the scan aborts
    early (used to interrupt a long background scan on window close). Rows
    written before the stop remain valid — they are decided/resumed normally.

    A persistent stat cache (``file_cache``) keyed by the card-relative path makes
    re-scans cheap: a file whose size+mtime match the cache reuses its stored hash
    and capture time with **no byte read**, so an unchanged card touches no file
    contents and a changed card reads only the new/modified files. The hash stays
    the ground-truth identity; the cache only decides whether we can skip reading.
    """
    # Resolve the target first: card_root_for resolves symlinks, so enumerating
    # the *unresolved* path (e.g. macOS's /tmp → /private/tmp) would yield files
    # that are "not relative to" the anchor and crash the relative-key math.
    root = Path(root).resolve()
    anchor = volume.card_root_for(root)       # stable base for the relative key
    paired = pairing.pair_folder(root)
    total = len(paired)
    cache = store.cached_files(volume_id) if volume_id is not None else {}
    seen_rel: set[str] = set()
    stopped = False

    # Phase 1: hash + capture time, upserting each shot *immediately* (group_id
    # left NULL) so the UI can show early shots before the scan finishes. NULL
    # groups sort first, i.e. discovery order, until phase 2 fills them in.
    items: list[GroupItem] = []
    for i, ps in enumerate(paired):
        if should_stop is not None and should_stop():
            stopped = True
            break
        # The identity source is the JPEG (or the RAW for a RAW-only orphan).
        src = ps.hash_source
        try:
            st = src.stat()
        except OSError:
            continue                          # vanished between listing and stat
        rel = str(src.relative_to(anchor))
        seen_rel.add(rel)

        cached = cache.get(rel)
        if cached is not None and cached[0] == st.st_size and cached[1] == st.st_mtime:
            # Unchanged: reuse the stored identity + time; no card read at all.
            h = cached[2]
            tval = cached[3] if cached[3] is not None else st.st_mtime
        else:
            # New or modified: read the bytes once and both hash and parse EXIF
            # from them — one card read per shot.
            try:
                data = src.read_bytes()       # read-only (G1)
                h = hashing.hash_bytes(data)
            except OSError:
                h = hashing.hash_file(src)    # fallback: stream from disk
                data = None
            ct = meta.read_capture_time(data) if (ps.has_jpg and data is not None) else None
            # Fall back to mtime so gap math always works; a misset-once clock
            # keeps intervals intact either way.
            tval = (float(ct.key[0]) + ct.subsec / 1000.0) if ct is not None else st.st_mtime
            if volume_id is not None:
                store.upsert_file_cache(
                    volume_id=volume_id, rel_path=rel,
                    size=st.st_size, mtime=st.st_mtime, hash=h,
                    capture_time=tval if ct is not None else None,
                )
        items.append(GroupItem(ident=h, time_value=tval, seq=_file_number(ps.stem)))
        store.upsert_scanned(
            hash=h,
            volume_id=volume_id,
            jpg_path=str(ps.jpg_path) if ps.jpg_path else None,
            raw_path=str(ps.raw_path) if ps.raw_path else None,
            group_id=None,
        )
        if progress:
            progress(i + 1, total)

    # Phase 2: assign time-gap groups for whatever was scanned (a stop leaves a
    # partial-but-consistent index; the rest fills in on the next scan).
    for h, gid in assign_groups(items, gap_s=gap_s).items():
        store.set_group(h, gid)
    # Forget cache rows for files that are gone now — but only after a full pass,
    # since an interrupted scan hasn't visited every file yet.
    if volume_id is not None and not stopped:
        store.prune_file_cache(volume_id, seen_rel)
    return len(items)


def rekey_shifted(
    store: Store,
    shots: list,
    volume_id: str | None,
    card_root: str | Path,
    offset_seconds: int,
    *,
    progress: ProgressFn | None = None,
) -> int:
    """After an in-place metadata edit (the clock-fix), move each shot's row to
    its file's new content hash — so decisions survive the byte change — and
    refresh the scan cache in the same pass.

    The cache refresh is what makes the post-fix rescan free: the new size,
    mtime, and hash are recorded here (the file had to be read to re-hash it
    anyway), and the cached capture time is shifted *arithmetically* by the same
    offset the files were — no EXIF re-parse, no second read. Without this, the
    next scan would re-read every shifted file from the card a second time.

    Returns the number of rows re-keyed.
    """
    anchor = Path(card_root).resolve()
    cache = store.cached_files(volume_id) if volume_id is not None else {}
    rekeyed = 0
    total = len(shots)
    for i, s in enumerate(shots, 1):
        src = s.jpg_path or s.raw_path          # the identity source (JPEG first)
        p = Path(src) if src else None
        if p is not None and p.exists():
            new_hash = hashing.hash_file(p)
            if store.rekey(s.hash, new_hash):
                rekeyed += 1
            if volume_id is not None:
                try:
                    rel = str(p.resolve().relative_to(anchor))
                    st = p.stat()
                except (OSError, ValueError):
                    pass                        # outside the anchor / vanished: no cache row
                else:
                    old = cache.get(rel)
                    old_ct = old[3] if old is not None else None
                    store.upsert_file_cache(
                        volume_id=volume_id, rel_path=rel,
                        size=st.st_size, mtime=st.st_mtime, hash=new_hash,
                        capture_time=(old_ct + offset_seconds)
                        if old_ct is not None else None,
                    )
        if progress is not None:
            progress(i, total)
    return rekeyed
