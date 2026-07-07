"""Restore chapter state from draft snapshots: whole chapter or one paragraph.

Restores are deterministic (no model calls) and write through the snapshot
chokepoint with reason `restore`, so the pre-restore state is itself
snapshotted and a bad restore is one more restore away. When an undetected
hand-edit is present (on-disk body differs from the manifest's last
recorded result hash), restore refuses without `force=True` -- the
chokepoint will preserve the hand-edit as a `human-edit` snapshot either
way; the flag is informed consent, not a data-loss switch.
"""

from __future__ import annotations

import difflib
import re

from ..ledger import Ledger
from ..project import ProjectError, WritingProject, split_frontmatter
from .snapshots import DraftStore, SnapshotEntry, body_hash, snapshot_write_chapter

# Paragraph alignment: the best-matching current paragraph must clear this
# SequenceMatcher ratio, and beat the runner-up by at least the margin,
# or the caller must disambiguate with an explicit position.
ALIGN_RATIO = 0.5
ALIGN_MARGIN = 0.1

_PARAGRAPH_SPLIT_RE = re.compile(r"\n\s*\n")


class RestoreError(RuntimeError):
    """Raised when a restore cannot proceed; the manuscript is untouched."""


def split_paragraphs(text: str) -> list[str]:
    return [p.strip() for p in _PARAGRAPH_SPLIT_RE.split(text) if p.strip()]


def load_snapshot(project: WritingProject, number: int, seq: int) -> tuple[SnapshotEntry, str]:
    """Manifest entry `seq` for a chapter plus its snapshot file text."""
    store = DraftStore(project)
    manifest = store.load_manifest(number)
    entry = next((e for e in manifest.entries if e.seq == seq), None)
    if entry is None:
        available = ", ".join(str(e.seq) for e in manifest.entries) or "none"
        raise RestoreError(
            f"No snapshot seq {seq} for ch-{number:02d} (available: {available}). "
            f"See: stoner drafts list {number}"
        )
    if entry.pruned or not entry.file:
        raise RestoreError(
            f"Snapshot seq {seq} for ch-{number:02d} has no stored content "
            f"({'pruned' if entry.pruned else 'body identical to the previous write'})."
        )
    path = store.chapter_dir(number) / entry.file
    if not path.exists():
        raise RestoreError(f"Snapshot file missing: {path.relative_to(project.root)}")
    return entry, path.read_text(encoding="utf-8")


def hand_edit_pending(project: WritingProject, number: int) -> bool:
    """True when the on-disk body differs from the last harness write."""
    manifest = DraftStore(project).load_manifest(number)
    if not manifest.last_result_sha256:
        return False
    try:
        _fm, body = project.read_chapter(number)
    except ProjectError:
        return False
    return body_hash(body) != manifest.last_result_sha256


def _guard_hand_edit(project: WritingProject, number: int, force: bool) -> None:
    if hand_edit_pending(project, number) and not force:
        raise RestoreError(
            f"ch-{number:02d} was hand-edited since the last harness write. "
            "Rerun with --force to proceed (the hand-edit will be preserved "
            "as a human-edit snapshot first)."
        )


def restore_chapter(project: WritingProject, number: int, seq: int, force: bool = False) -> str:
    """Restore a whole chapter from snapshot `seq`. Returns the written rel path."""
    _entry, text = load_snapshot(project, number, seq)
    _guard_hand_edit(project, number, force)
    fm, body = split_frontmatter(text)
    snapshot_write_chapter(
        project, number, fm, body, reason="restore", detail={"from_seq": seq}
    )
    rel = project.chapter_rel(number)
    Ledger(project.root).append("drafts.restore", target=rel, chapter=number, seq=seq)
    return rel


def align_paragraph(current_paragraphs: list[str], target: str) -> int:
    """Index of the current paragraph best matching `target`.

    Deterministic SequenceMatcher alignment; raises `RestoreError` when no
    paragraph clears ALIGN_RATIO or two candidates are too close to call.
    """
    if not current_paragraphs:
        raise RestoreError("current chapter has no paragraphs to align against")
    key = " ".join(target.split())
    ratios = [
        difflib.SequenceMatcher(None, key, " ".join(p.split())).ratio()
        for p in current_paragraphs
    ]
    ranked = sorted(range(len(ratios)), key=lambda i: ratios[i], reverse=True)
    best = ranked[0]
    if ratios[best] < ALIGN_RATIO:
        raise RestoreError(
            "could not align the snapshot paragraph to the current body "
            f"(best match ratio {ratios[best]:.2f}); pass --at <paragraph> "
            "to say where it goes"
        )
    if len(ranked) > 1 and ratios[best] - ratios[ranked[1]] < ALIGN_MARGIN:
        raise RestoreError(
            "ambiguous paragraph alignment: paragraphs "
            f"{best + 1} and {ranked[1] + 1} match almost equally well; "
            "pass --at <paragraph> to choose"
        )
    return best


def restore_paragraph(
    project: WritingProject,
    number: int,
    seq: int,
    paragraph: int,
    at: int | None = None,
    force: bool = False,
) -> str:
    """Restore paragraph `paragraph` (1-based) of snapshot `seq` into the
    current body, replacing the best-aligned current paragraph (or the
    explicit 1-based `at` position). Returns the written rel path."""
    _entry, text = load_snapshot(project, number, seq)
    _snap_fm, snap_body = split_frontmatter(text)
    snap_paragraphs = split_paragraphs(snap_body)
    if not 1 <= paragraph <= len(snap_paragraphs):
        raise RestoreError(
            f"snapshot seq {seq} has {len(snap_paragraphs)} paragraph(s); "
            f"--paragraph {paragraph} is out of range"
        )
    target = snap_paragraphs[paragraph - 1]

    fm, body = project.read_chapter(number)
    current = split_paragraphs(body)
    if at is not None:
        if not 1 <= at <= len(current):
            raise RestoreError(
                f"current ch-{number:02d} has {len(current)} paragraph(s); "
                f"--at {at} is out of range"
            )
        index = at - 1
    else:
        index = align_paragraph(current, target)

    _guard_hand_edit(project, number, force)
    new_paragraphs = list(current)
    new_paragraphs[index] = target
    new_body = "\n\n".join(new_paragraphs) + "\n"
    snapshot_write_chapter(
        project,
        number,
        fm,
        new_body,
        reason="restore",
        detail={"from_seq": seq, "paragraph": paragraph, "at": index + 1},
    )
    rel = project.chapter_rel(number)
    Ledger(project.root).append(
        "drafts.restore",
        target=rel,
        chapter=number,
        seq=seq,
        paragraph=paragraph,
        at=index + 1,
    )
    return rel
