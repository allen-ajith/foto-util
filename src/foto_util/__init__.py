"""foto-util — non-destructive, keyboard-driven photo culling for Sony RAW+JPEG cards.

The package is split into small modules that cooperate only through the off-card
SQLite store (see :mod:`foto_util.store`); the store is the seam and parts never call
each other directly. The single module permitted to mutate the source volume is
:mod:`foto_util.fileops`.
"""

__version__ = "0.1.0"
