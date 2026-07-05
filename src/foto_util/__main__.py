"""Command-line entry point: ``foto-util <folder>``.

Resolves the volume, opens the off-card store, and launches the viewer. The
viewer runs the scan on a background thread so the first shot appears before
indexing finishes (design doc §10). ``--no-gui`` does a synchronous scan and
prints a summary instead — handy for smoke-testing without a display.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import indexer, volume
from .appdir import db_path
from .grouping import SCENE_GAP_S
from .store import Store


def _print_progress(done: int, total: int) -> None:
    pct = (done / total * 100) if total else 100
    print(f"\r  scanning {done}/{total} ({pct:3.0f}%)", end="", file=sys.stderr)
    if done >= total:
        print(file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="foto-util",
        description="Non-destructive, keyboard-driven culling of Sony RAW+JPEG cards.",
    )
    parser.add_argument(
        "path", nargs="?", default=None,
        help="folder to cull (a mounted card's DCIM, or any folder). "
             "Omit to pick a card/folder from a startup menu.",
    )
    parser.add_argument(
        "--no-gui", action="store_true",
        help="scan and print a summary, then exit (no window)",
    )
    parser.add_argument(
        "--scene-gap", type=float, default=SCENE_GAP_S,
        help=f"seconds of gap that starts a new scene (default {SCENE_GAP_S:g})",
    )
    args = parser.parse_args(argv)

    if args.no_gui:
        if args.path is None:
            print("error: --no-gui requires a folder path", file=sys.stderr)
            return 2
        target = Path(args.path).expanduser().resolve()
        if not target.exists():
            print(f"error: {target} does not exist", file=sys.stderr)
            return 2
        root = volume.card_root_for(target)
        vol = volume.source_id_for(root)
        strict = volume.is_card(root)
        store = Store(db_path())
        n = indexer.scan(target, store, vol, gap_s=args.scene_gap,
                         progress=_print_progress)
        c = store.counts(vol)
        print(f"{n} shots: {c['undecided']} undecided, {c['decided']} decided")
        print(f"volume: {vol}")
        print(f"card root: {root}  (strict card mode: {strict})")
        store.close()
        return 0

    # GUI: a given path launches straight in; no path shows the source picker.
    target = None
    if args.path is not None:
        target = Path(args.path).expanduser().resolve()
        if not target.exists():
            print(f"error: {target} does not exist", file=sys.stderr)
            return 2
    try:
        from .viewer import launch  # imported lazily so non-GUI use needs no Qt
    except ImportError as e:  # pragma: no cover - depends on optional GUI deps
        print(f"error: GUI unavailable ({e}). Try --no-gui.", file=sys.stderr)
        return 1
    return launch(target, gap_s=args.scene_gap)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
