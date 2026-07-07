"""Draft archaeology: preserve every chapter body the harness overwrites.

Public surface:

- :func:`snapshot_write_chapter` -- the snapshotting write chokepoint every
  rewrite path calls in place of `WritingProject.write_chapter`.
- :class:`DraftStore` -- the `.stoner/drafts/` per-chapter snapshot store
  (manifest + snapshot files) behind it.
"""

from __future__ import annotations

from .snapshots import ChapterManifest, DraftStore, SnapshotEntry, snapshot_write_chapter

__all__ = [
    "ChapterManifest",
    "DraftStore",
    "SnapshotEntry",
    "snapshot_write_chapter",
]
