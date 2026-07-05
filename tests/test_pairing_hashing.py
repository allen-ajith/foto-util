"""Pairing (folder-scoped stems, orphans) and content hashing."""

from __future__ import annotations

from foto_util import hashing, pairing
from tests.make_fixture import SHOTS


def test_pairs_match_fixture_spec(card):
    shots = pairing.pair_folder(card / "DCIM")
    # One PairedShot per spec entry, keyed by (folder, stem).
    got = {(s.folder.name, s.stem): s for s in shots}
    assert len(got) == len(SHOTS)

    for spec in SHOTS:
        ps = got[(spec.folder, spec.stem)]
        assert ps.has_jpg is spec.has_jpg
        assert ps.has_raw is spec.has_raw
        assert ps.is_orphan is (spec.kind != "pair")


def test_pairing_skips_hidden_directories(card):
    """Finder-deleted photos live in the card's hidden ``.Trashes`` — files under
    any dot-directory must not reappear as phantom shots to cull."""
    hidden = card / ".Trashes" / "501"
    hidden.mkdir(parents=True)
    (hidden / "DSC09999.JPG").write_bytes(b"finder-deleted photo")

    shots = pairing.pair_folder(card)
    assert not any(s.stem == "DSC09999" for s in shots)


def test_pairing_skips_appledouble_and_dotfiles(card):
    """macOS ``._*`` AppleDouble sidecars (and other dotfiles) carry an image
    extension but are metadata, not photos — they must never pair into shots."""
    folder = card / "DCIM" / "100MSDCF"
    (folder / "._DSC00001.JPG").write_bytes(b"AppleDouble junk")
    (folder / "._DSC00001.ARW").write_bytes(b"AppleDouble junk")
    (folder / ".DS_Store").write_bytes(b"junk")

    shots = pairing.pair_folder(card / "DCIM")
    assert all(not s.stem.startswith(".") for s in shots)        # no phantom shots
    assert "DSC00001" in {s.stem for s in shots}                 # the real one survives


def test_pairing_skips_management_folder_thumbnails(card):
    """Sony stores video-clip thumbnails as PRIVATE/M4ROOT/THMBNL/C0001T01.JPG —
    a .JPG by extension but a management-folder file, never a photo to cull. Even
    scanning the whole card root must skip anything under a management folder."""
    thmbnl = card / "PRIVATE" / "M4ROOT" / "THMBNL"
    thmbnl.mkdir(parents=True, exist_ok=True)
    (thmbnl / "C0001T01.JPG").write_bytes(b"fake video thumbnail")

    shots = pairing.pair_folder(card)          # scan the card ROOT, not just DCIM
    assert all("THMBNL" not in str(s.folder) for s in shots)
    assert all(not s.stem.startswith("C0001T01") for s in shots)
    assert any(s.stem == "DSC00001" for s in shots)   # real DCIM photos still found


def test_duplicate_stem_across_folders_pairs_independently(card):
    shots = pairing.pair_folder(card / "DCIM")
    dsc1 = [s for s in shots if s.stem == "DSC00001"]
    assert {s.folder.name for s in dsc1} == {"100MSDCF", "101MSDCF"}
    # Same stem, different folders → different files → different hashes.
    h = {hashing.hash_file(s.hash_source) for s in dsc1}
    assert len(h) == 2


def test_hash_source_prefers_jpeg(card):
    shots = {s.stem: s for s in pairing.pair_folder(card / "DCIM")}
    pair = shots["DSC00020"]
    assert pair.hash_source == pair.jpg_path
    orphan_raw = shots["DSC00011"]
    assert orphan_raw.hash_source == orphan_raw.raw_path


def test_hash_is_stable_and_readonly(card):
    arw = card / "DCIM" / "100MSDCF" / "DSC00001.ARW"
    before = arw.stat().st_mtime
    h1 = hashing.hash_file(arw)
    h2 = hashing.hash_file(arw)
    assert h1 == h2
    assert arw.stat().st_mtime == before  # reading never writes (G1)


def test_hash_bytes_matches_hash_file(card):
    """The single-read scan path must produce the same identity as hash_file."""
    jpg = card / "DCIM" / "100MSDCF" / "DSC00001.JPG"
    data = jpg.read_bytes()
    assert hashing.hash_bytes(data) == hashing.hash_file(jpg)


def test_display_exif_separates_capture_time_from_settings(card):
    """Capture date/time is shown month-day-year in its own field, kept separate
    from the camera-settings line so the top bar isn't cluttered."""
    from foto_util import meta

    jpg = card / "DCIM" / "100MSDCF" / "DSC00001.JPG"
    de = meta.read_display_exif(jpg)
    assert de.when is not None
    ws = de.when_str()
    assert ws.startswith(de.when.strftime("%b"))   # month name leads (month-day-year)
    assert str(de.when.year) in ws
    assert "ISO" in de.one_line()                   # settings line still has the camera info
    assert "ISO" not in ws and "f/" not in ws       # date is separate from the settings


def test_capture_time_from_bytes_matches_path(card):
    from foto_util import meta

    jpg = card / "DCIM" / "100MSDCF" / "DSC00001.JPG"
    from_path = meta.read_capture_time(jpg)
    from_bytes = meta.read_capture_time(jpg.read_bytes())
    assert from_path is not None
    assert from_bytes is not None
    assert (from_bytes.when, from_bytes.subsec) == (from_path.when, from_path.subsec)
