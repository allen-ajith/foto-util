"""Fast content hashing (xxhash).

A shot's identity is the hash of its JPEG bytes (falling back to the RAW for a
JPEG-only orphan). That identity drives the already-handled guard and resume, so
a re-inserted or partly-culled card jumps straight to undecided shots. The same
hash is used to verify a trash copy before unlinking the original (guard G5).
"""

from __future__ import annotations

from pathlib import Path

import xxhash

# Read in modest chunks so we never load a whole RAW/JPEG into memory.
_CHUNK = 1 << 20  # 1 MiB


def hash_file(path: str | Path) -> str:
    """Return a hex xxh3-64 digest of the file's full contents (read-only)."""
    h = xxhash.xxh3_64()
    # Opened read-only; decoding/inspection never writes to the source (G1).
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(_CHUNK), b""):
            h.update(chunk)
    return h.hexdigest()


def hash_bytes(data: bytes) -> str:
    """Digest of in-memory bytes — identical to :func:`hash_file` of the same
    content. Lets the scanner read a JPEG once and both hash and parse it from
    the same bytes, halving card I/O."""
    return xxhash.xxh3_64(data).hexdigest()
