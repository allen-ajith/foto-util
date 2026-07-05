# foto-util

Fast, keyboard-driven, non-destructive photo culling for Sony cards with RAW+JPEGs.

## Why I built this

I got a new camera, forgot to set its date and time, and then shot a whole trip in
RAW+JPEG. I came back with hundreds of photos that all carried the wrong timestamps, and I
needed to do two things quickly: throw out the bad frames, and fix the dates. I did not want
a heavy photo manager, and I definitely did not want anything that reorganizes or rewrites my
memory card. So I built foto-util. It shows me each shot, lets me keep or drop it with a
single keypress, and can shift every capture time by a fixed offset, all without changing the
structure of the card.

## What it does

Point it at a card or a folder. It shows each shot full screen. For every shot you press one
key: keep both files, keep just the JPEG (and drop the RAW), or delete the shot. After each
decision it pauses briefly so you can see what happened, then moves to the next undecided
shot. Culling a full card is mostly tapping `1`, `2`, `3`.

The one hard rule: it never reorganizes or rewrites your card. During culling, the only
change it ever makes to the card is removing a file you rejected, and even that is staged
and reversible. All of its own data lives off the card. The one deliberate exception is the
opt-in clock-fix tool, which — behind a confirm dialog — rewrites capture dates in place and
keeps a verified backup of every original.

## Tested on

* Camera: Sony a6400 (RAW files are `.ARW`).
* OS: macOS Sequoia

It has not been tested on other cameras or other operating systems yet. The brand-specific
parts are small and isolated (mainly the card folder layout and the RAW file extension), so
other brands are very doable. See [Contributing](#contributing).

## Light by design

* Three small runtime libraries: PySide6 (the window), xxhash (fast content fingerprints),
  and piexif (reading capture dates; a small, stable library — note it is no longer actively
  maintained, which is fine for reading Sony JPEGs but worth knowing).
* One local SQLite file holds all of its state. 
* The published package ships only the app code. Tests and dev tooling are not included in it.

## Install

You need [uv](https://docs.astral.sh/uv/) (it installs and manages the right Python for you).
macOS is the primary target.

```sh
uv tool install foto-util
# or from a local clone:
git clone <this-repo> && cd foto-util && uv tool install .
```

That puts a `foto-util` command on your PATH. Optional, only for the clock-fix tools:

```sh
brew install exiftool
```

## Run

```sh
foto-util                       # picker of mounted cards and folders
foto-util /Volumes/YOURCARD     # jump straight into a card
foto-util ~/some/folder         # or any folder of photos
foto-util <folder> --no-gui     # scan and print a summary, no window
```

The app opens full screen. Press `F` for a normal, resizable window and back.

## Keys

The three decisions:

| Key | Decision |
|-----|----------|
| `1` | Keep the JPEG, send the RAW to the trash (only for a RAW+JPEG pair). |
| `2` | Keep both files, nothing moved. |
| `3` | Delete the shot (RAW and/or JPEG go to the trash). |

Everything else:

| Key | Action |
|-----|--------|
| `4` | Zoom to 100% at the cursor (mouse wheel zooms, drag pans). |
| `R` | Rotate the view 90 degrees (display only, the file is untouched). |
| `F` | Toggle full screen and a resizable window. |
| `Left` / `Right` / `Space` | Previous / next / next shot. |
| `U` | Undo the last decision (brings trashed files back). |
| `[` / `]` | Jump to the previous / next time group (a scene or burst). |
| `?` or `H` | Show or hide the on-screen key hints. |
| `Cmd E` | Eject the card. |

## Menus

* **File**
  * Open card or folder: switch source without restarting.
  * Recover trashed files to card: put everything in this card's trash back on the card. 
  * Empty trash (permanently delete): the point of no return. Until you run this, every
    deleted file is still recoverable. Emptied shots leave the roll entirely, and a
    keep-JPEG shot becomes plain JPEG-only (its RAW is gone for good).
  * Storage: see how much disk each card's trash takes (including cards not currently
    inserted) and empty the open card's trash from there. Another card's trash can only be
    emptied by opening that card first.
* **Database** (housekeeping only, never touches a photo)
  * Prune stale rows: forget entries whose files are truly gone.
  * Forget this card: clear this card's entries and re-scan it fresh (the scan stays fast —
    the file fingerprints are kept).
  * Clear all: reset the whole database, including the scan cache, so the next scan re-reads
    every file. Forget and Clear refuse to run while files are still in the trash, so nothing
    gets stranded.
* **Tools** (need exiftool)
  * Fix clock offset: pick any shot as an anchor, enter its true date and time, and every shot
    on the card shifts by that same offset (relative timing is preserved). For when the camera
    clock was set wrong. Keeps a verified backup of each original.
  * Verify and clear clock-fix backups: reclaim the space those backups take, after confirming
    each one still matches.

## How it works under the hood

The design is a few simple, careful ideas.

### The card is read-only except for staged deletions
foto-util reads the card (lists files, reads photos to show them, reads the embedded date and
time). During culling, the only write it ever makes to the card is removing an image file you
rejected. It never renames, moves, re-folders, or rewrites anything else. Its own data (the
database and the trash) lives off the card, in `~/Library/Application Support/foto-util/`.
The opt-in clock-fix tool is the exception, and it says so before it runs: it rewrites the
dates inside each photo (keeping a `_original` backup next to it) — see below.

### Identity is the file content, not the name
Each shot is identified by a fast fingerprint (an xxhash) of the JPEG's bytes. The filename and
folder do not define the shot; the content does. So you can eject and re-insert the card, mount
it under a different drive name, even reformat and re-copy, and foto-util still recognizes the
same shots and remembers your decisions.

### One source of truth
All state (every decision, every file location, the time groups) lives in one local SQLite
database. The scanner, the viewer, and the file-moving code do not call each other; they only
read and write this database. Fewer connections between parts means fewer ways to reach a bad
state.

### Deletes are staged, verified, and reversible
When you drop a file, foto-util copies it to the off-card trash, verifies the copy byte for byte
(size and fingerprint), and only then removes the original from the card. If the process dies at
any point before the removal, the original is still on the card. Nothing is permanently gone
until you choose Empty trash.

### Undo survives a restart
Your decisions and the trashed files both live on disk, so Undo works even after you quit and
reopen the app. It does not rely on in-memory history; it looks at what is actually recoverable.
Every restore — single-shot undo, re-deciding a shot, and the bulk Recover — refuses to
overwrite a file already on the card whose content differs (say the camera reused a filename
after you culled): the card version is kept, the trashed copy stays in the trash, and the
conflict is reported instead of resolved by force.

### Re-scans are cheap
The first time it sees a card, foto-util reads each photo once to fingerprint it. After that it
remembers each file's size and modified time. Reopen the same card and it reads nothing it has
already seen; only new or changed photos are read again. So the second open of a large card is
quick instead of re-reading tens of gigabytes. (The standard stat-cache trade-off applies: a
file replaced in place with identical size *and* timestamp would keep its old fingerprint —
practically impossible for camera files, and dropping the database forces a full re-read.)

### Grouping by time gaps
foto-util clusters shots into scenes and bursts by the gaps between capture times, shows the
current group as a filmstrip, and lets you jump between groups.

### Fixing a wrong camera clock
Put any shot on screen as an anchor and enter the true date and time for that one shot. foto-util takes the difference from the anchor's recorded time
and shifts the stored dates on every shot by that same amount, so the whole set moves together
and the relative timing between shots stays intact. The shift runs through exiftool
and keeps a verified backup of each original. Verify and clear clock-fix backups later reclaims
that space. It confirms each backup matches the live file by a hash of the image data only (which
the date edit does not change) before deleting it, so it never drops a backup that does not match.

### A tiny, auditable delete surface
During culling, the only files foto-util will ever remove are image files inside
`DCIM/<folder>/`. The camera's management folders (thumbnails, indexes, video sidecars) are
hard-blocked. The clock-fix tools add two narrow, confirmed exceptions on top of that: clearing
verified `_original` backups (each checked against its live photo first), and removing the
`._` metadata sidecars macOS creates for the exact files the fix rewrote — never a card-wide
sweep.

## Limitations

* Byte-identical duplicates collapse into one shot. Identity is the content fingerprint, so
  two copies of the exact same JPEG on one card are a single entry — the second copy is
  neither shown nor removed. Distinct photos (even from the same burst) are never affected.
* Files you deleted through Finder (in the card's hidden `.Trashes`) are ignored entirely —
  they are Finder's business, not foto-util's.

## Contributing

Right now foto-util targets Sony (`.ARW`) on macOS. I would love to see versions for other
brands: Canon (`.CR3`, `.CR2`), Nikon (`.NEF`), Fujifilm (`.RAF`), and so on, and support for
Linux and Windows. The brand-specific parts are small and isolated (the card layout in
`pairing.py` and `safety.py`, and the RAW extension), so adding a brand is mostly local work
plus a test. If you want to contribute support for your camera or platform, please open a pull
request. Bug reports and fixes are very welcome too.

## Development

```sh
git clone <this-repo> && cd foto-util
uv sync --extra dev      # create the venv (dev extra adds pytest and Pillow)
uv run foto-util         # run from source
uv run pytest            # the test suite
```

The tests run against a synthetic fixture card built on the fly (`tests/make_fixture.py`) that
mirrors a real Sony a6400 layout, so no physical card is needed. The offscreen GUI tests
dispatch real key events, so behavior is tested the way you would actually use it.

### Project layout

```
src/foto_util/
  __main__.py    command-line entry point
  viewer.py      the PySide6 window and source picker (the whole UI)
  controller.py  the cull loop: decisions to file operations, undo, resume
  store.py       the SQLite database, the single source of truth
  indexer.py     read-only scan: pair, fingerprint, date, group, save (with the cache)
  fileops.py     the only code that changes the card: staged trash, restore, recover
  safety.py      the guard for what is allowed to be deleted
  pairing.py     group files into shots by name (RAW plus JPEG)
  hashing.py     the content fingerprint (xxhash)
  grouping.py    cluster shots into scenes and bursts by time gaps
  meta.py        read EXIF, optional exiftool clock shift
  backups.py     verify and clear clock-fix backups
  volume.py      identify the card or volume, list sources for the picker
  appdir.py      locate the off-card app directory
  model.py       the core data types
```

## License

[MIT](LICENSE).
