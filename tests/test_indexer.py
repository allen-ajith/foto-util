"""Progressive indexing: rows appear during the scan (group_id filled second)."""

from __future__ import annotations

from pathlib import Path

import piexif

from foto_util import hashing, indexer, volume


def _count_reads(monkeypatch) -> dict:
    """Count file-content reads (Path.read_bytes) during a scan."""
    counts = {"n": 0}
    orig = Path.read_bytes

    def counting(self):
        counts["n"] += 1
        return orig(self)

    monkeypatch.setattr(Path, "read_bytes", counting)
    return counts


def test_rekey_shifted_updates_rows_and_cache(card, store, monkeypatch):
    """After an in-place edit (the clock-fix), rekey_shifted must move the row to
    the new content hash AND refresh the scan cache — new stat + hash, capture
    time shifted arithmetically — so the next scan reads zero bytes."""
    vol = volume.volume_id_for(card)
    indexer.scan(card / "DCIM", store, vol)
    shots = store.shots(vol)
    jpg = card / "DCIM" / "100MSDCF" / "DSC00001.JPG"
    target = next(s for s in shots if s.jpg_path and s.jpg_path.endswith("DSC00001.JPG"))
    old_hash = target.hash
    rel = str(jpg.resolve().relative_to(card.resolve()))
    old_ct = store.cached_files(vol)[rel][3]
    assert old_ct is not None                       # fixture JPEGs carry EXIF time

    # simulate the metadata edit: bytes change in place → identity hash changes
    jpg.write_bytes(jpg.read_bytes() + b"\x00")
    offset = 3600

    n = indexer.rekey_shifted(store, shots, vol, card, offset)

    assert n == 1                                   # only the edited file re-keyed
    assert store.get(old_hash) is None
    new_hash = hashing.hash_file(jpg)
    assert store.get(new_hash) is not None
    st = jpg.stat()
    size, mtime, cached_hash, ct = store.cached_files(vol)[rel]
    assert (size, mtime, cached_hash) == (st.st_size, st.st_mtime, new_hash)
    assert ct == old_ct + offset                    # shifted arithmetically, no re-parse

    # the payoff: a rescan after the fix reads no file contents at all
    reads = _count_reads(monkeypatch)
    indexer.scan(card / "DCIM", store, vol)
    assert reads["n"] == 0


def test_scan_through_a_symlinked_path(card, store, tmp_path):
    """macOS paths like /tmp resolve elsewhere (/private/tmp); scanning through
    a symlink must not crash the relative-key math (enumeration and the cache
    anchor must agree on the resolved root)."""
    link = tmp_path / "card-link"
    link.symlink_to(card)
    vol = volume.volume_id_for(card)

    n = indexer.scan(link / "DCIM", store, vol)

    assert n == 7
    assert store.counts(vol)["total"] == 7


def test_rescan_unchanged_reads_no_bytes(card, store, monkeypatch):
    """The whole point of the cache: a second scan of an unchanged card reads
    zero file contents and yields an identical index."""
    vol = volume.volume_id_for(card)
    indexer.scan(card / "DCIM", store, vol)                 # cold: populates cache
    before = {s.hash: (s.jpg_path, s.raw_path, s.group_id) for s in store.shots(vol)}

    reads = _count_reads(monkeypatch)
    n = indexer.scan(card / "DCIM", store, vol)             # warm: all cache hits

    assert reads["n"] == 0                                  # no card reads at all
    assert n == 7
    after = {s.hash: (s.jpg_path, s.raw_path, s.group_id) for s in store.shots(vol)}
    assert after == before                                  # identical index


def test_rescan_reads_only_the_new_file(card, store, monkeypatch):
    """Adding one shot reads only that file — not the whole card."""
    vol = volume.volume_id_for(card)
    indexer.scan(card / "DCIM", store, vol)

    new = card / "DCIM" / "100MSDCF" / "DSC09999.JPG"
    src = card / "DCIM" / "100MSDCF" / "DSC00001.JPG"
    new.write_bytes(src.read_bytes() + b"\x00")            # valid JPEG, distinct hash

    reads = _count_reads(monkeypatch)
    n = indexer.scan(card / "DCIM", store, vol)

    assert reads["n"] == 1                                  # only the new file read
    assert n == 8
    assert store.get(hashing.hash_file(new)) is not None    # the new shot is indexed


def test_rescan_rehashes_a_modified_file(card, store, monkeypatch):
    """A changed size+mtime busts the cache for that file — it is re-read and
    re-identified, never trusted blindly."""
    vol = volume.volume_id_for(card)
    indexer.scan(card / "DCIM", store, vol)
    target = card / "DCIM" / "100MSDCF" / "DSC00001.JPG"
    target.write_bytes(target.read_bytes() + b"\x00")      # mutate content + stat

    reads = _count_reads(monkeypatch)
    indexer.scan(card / "DCIM", store, vol)

    assert reads["n"] == 1                                  # only the changed file
    assert store.get(hashing.hash_file(target)) is not None  # new identity indexed


def test_removed_file_dropped_from_cache(card, store):
    """A file gone from the card is pruned from the cache on the next full scan."""
    vol = volume.volume_id_for(card)
    indexer.scan(card / "DCIM", store, vol)
    assert any("DSC00002" in rp for rp in store.cached_files(vol))

    (card / "DCIM" / "100MSDCF" / "DSC00002.JPG").unlink()
    (card / "DCIM" / "100MSDCF" / "DSC00002.ARW").unlink()
    indexer.scan(card / "DCIM", store, vol)

    assert not any("DSC00002" in rp for rp in store.cached_files(vol))


def test_scan_aborts_when_should_stop(card, store):
    """should_stop halts the scan early, leaving a partial-but-valid index."""
    vol = volume.volume_id_for(card)
    seen = {"n": 0}

    def stop_after_two(done: int, total: int) -> None:
        seen["n"] = done

    # stop once two shots are in
    n = indexer.scan(card / "DCIM", store, vol,
                     progress=stop_after_two,
                     should_stop=lambda: seen["n"] >= 2)

    assert n == 2                              # processed only two shots
    assert store.counts(vol)["total"] == 2     # rows for those two exist & resumable


def test_scan_reads_each_jpeg_only_once(card, store, monkeypatch):
    """The scan must hash and parse EXIF from a single read per JPEG — never a
    second full read off the (slow) card. Guards the single-read optimization."""
    reads = {"jpeg_full": 0, "piexif_from_path": 0, "piexif_from_bytes": 0}

    orig_read_bytes = Path.read_bytes

    def counting_read_bytes(self):
        if self.suffix.upper() == ".JPG":
            reads["jpeg_full"] += 1
        return orig_read_bytes(self)

    orig_load = piexif.load

    def counting_load(src, *a, **k):
        if isinstance(src, (bytes, bytearray)):
            reads["piexif_from_bytes"] += 1
        else:
            reads["piexif_from_path"] += 1
        return orig_load(src, *a, **k)

    monkeypatch.setattr(Path, "read_bytes", counting_read_bytes)
    monkeypatch.setattr(piexif, "load", counting_load)

    vol = volume.volume_id_for(card)
    indexer.scan(card / "DCIM", store, vol)

    njpg = len(list((card / "DCIM").rglob("*.JPG")))
    assert reads["jpeg_full"] == njpg          # exactly one read per JPEG
    assert reads["piexif_from_path"] == 0      # EXIF never triggers a 2nd read
    assert reads["piexif_from_bytes"] == njpg  # parsed from the same bytes


def test_rows_appear_incrementally_during_scan(card, store):
    vol = volume.volume_id_for(card)
    counts: list[int] = []

    def progress(done: int, total: int) -> None:
        counts.append(store.counts(vol)["total"])
        if done == total:
            # still phase 1 — groups not assigned yet, so the UI can already
            # show shots before grouping finishes.
            assert all(s.group_id is None for s in store.shots(vol))

    total = indexer.scan(card / "DCIM", store, vol, progress=progress)

    assert total == 7
    assert counts[0] >= 1                 # first shot visible after one tick
    assert counts == sorted(counts)       # monotonic: rows only accumulate
    assert counts[-1] == 7
    # phase 2 done: every shot now has a group
    assert all(s.group_id is not None for s in store.shots(vol))


def test_groups_reflect_scene_structure(card, store):
    vol = volume.volume_id_for(card)
    indexer.scan(card / "DCIM", store, vol)
    # key by folder/name — DSC00001 exists in both 100MSDCF and 101MSDCF
    by_name = {
        "/".join((s.jpg_path or s.raw_path).split("/")[-2:]): s.group_id
        for s in store.shots(vol)
    }
    # the 3-frame burst shares a group; the +10min/+20min shots split off
    assert (by_name["100MSDCF/DSC00001.JPG"]
            == by_name["100MSDCF/DSC00002.JPG"]
            == by_name["100MSDCF/DSC00003.JPG"])
    assert by_name["100MSDCF/DSC00010.JPG"] != by_name["100MSDCF/DSC00001.JPG"]
    assert by_name["100MSDCF/DSC00020.JPG"] != by_name["100MSDCF/DSC00010.JPG"]
