"""Draft snapshot store and the snapshotting chapter-write chokepoint.

Every harness rewrite of a manuscript chapter goes through
:func:`snapshot_write_chapter` instead of calling
`WritingProject.write_chapter` directly. Before the new body lands, the
prior chapter text (frontmatter + body) is preserved under
`.stoner/drafts/ch-NN/<seq>-<reason>.md`, with one JSON manifest per
chapter recording reason, timestamp, body hash, session ref, and
caller-supplied detail. `reason` names the rewrite event that displaced the
content (`draft`, `slop-revise`, `review-revise`, `book-revise`, ...);
reasons are free-form strings by convention, mirroring the ledger's
no-registry action names, so other features can adopt the chokepoint with
their own reason without touching this module.

Hashes cover the body only (sha256), so frontmatter churn (status bump,
recomputed word count) neither triggers a false `human-edit` detection nor
defeats dedup. Human hand-edits made between harness runs are detected by
comparing the on-disk body hash against the manifest's last recorded result
hash, and are snapshotted with reason `human-edit` before anything
overwrites them. Git is enrichment, never a dependency: when the project
root is a git repo, each entry records the current HEAD commit; any failure
silently omits the field.
"""

from __future__ import annotations

import hashlib
import re
import subprocess
import time
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, ValidationError

from ..ledger import Ledger
from ..project import ProjectError, WritingProject, split_frontmatter

MANIFEST_NAME = "manifest.json"

_GIT_TIMEOUT_SECONDS = 5.0
_SLUG_RE = re.compile(r"[^a-z0-9]+")


def body_hash(body: str) -> str:
    """sha256 over the chapter body only (frontmatter excluded)."""
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def slugify_reason(reason: str) -> str:
    slug = _SLUG_RE.sub("-", reason.lower()).strip("-")
    return slug or "snapshot"


def _git_head(root: Path) -> str | None:
    """Best-effort HEAD commit; any failure means no field, never an error."""
    if not (root / ".git").exists():
        return None
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    head = proc.stdout.strip()
    return head or None


class SnapshotEntry(BaseModel):
    """One manifest row: a body state displaced by the named rewrite event."""

    seq: int
    ts: float = Field(default_factory=time.time)
    reason: str = ""
    sha256: str = ""  # hash of the snapshotted (displaced) body
    result_sha256: str = ""  # hash of the body the event wrote
    file: str = ""  # snapshot filename in the chapter dir; "" when deduped away
    session: str = ""
    detail: dict[str, Any] = Field(default_factory=dict)
    git_head: str | None = None
    pruned: bool = False


class ChapterManifest(BaseModel):
    chapter: int
    last_result_sha256: str = ""  # what drift detection compares against
    entries: list[SnapshotEntry] = Field(default_factory=list)


class DraftStore:
    """Owns `.stoner/drafts/ch-NN/`: manifest read/write and snapshot files."""

    def __init__(self, project: WritingProject):
        self.project = project

    def chapter_dir(self, number: int) -> Path:
        return self.project.root / ".stoner" / "drafts" / f"ch-{number:02d}"

    def manifest_path(self, number: int) -> Path:
        return self.chapter_dir(number) / MANIFEST_NAME

    def load_manifest(self, number: int) -> ChapterManifest:
        """Load a chapter manifest, tolerating corruption: a file that will
        not parse is backed up to `manifest.json.bak` and a fresh manifest
        is returned (mirrors `pipelines.book.load_state`)."""
        p = self.manifest_path(number)
        if not p.exists():
            return ChapterManifest(chapter=number)
        try:
            return ChapterManifest.model_validate_json(p.read_text(encoding="utf-8"))
        except (ValidationError, ValueError, OSError):
            backup = p.parent / (p.name + ".bak")
            try:
                p.replace(backup)
            except OSError:
                pass
            return ChapterManifest(chapter=number)

    def save_manifest(self, number: int, manifest: ChapterManifest) -> Path:
        p = self.manifest_path(number)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(
            manifest.model_dump_json(indent=2, exclude_none=True), encoding="utf-8"
        )
        return p

    def snapshot_filename(self, seq: int, reason: str) -> str:
        return f"{seq:02d}-{slugify_reason(reason)}.md"

    def record(
        self,
        number: int,
        manifest: ChapterManifest,
        full_text: str,
        sha: str,
        reason: str,
        session: str = "",
        detail: dict[str, Any] | None = None,
        git_head: str | None = None,
        result_sha256: str = "",
        skip_file: bool = False,
    ) -> SnapshotEntry:
        """Append a manifest entry for `full_text` (whose body hashes to
        `sha`), writing the snapshot file unless dedup applies: an identical
        consecutive body reuses the previous entry's file, and `skip_file`
        (body unchanged by the write) records the entry with no file at all.
        """
        seq = len(manifest.entries) + 1
        dedup = self.project.config.archaeology.dedup
        last = manifest.entries[-1] if manifest.entries else None
        if dedup and last is not None and last.sha256 == sha and last.file:
            file = last.file  # identical consecutive body: point at existing file
        elif dedup and skip_file:
            file = ""
        else:
            file = self.snapshot_filename(seq, reason)
            path = self.chapter_dir(number) / file
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(full_text, encoding="utf-8")
        entry = SnapshotEntry(
            seq=seq,
            reason=reason,
            sha256=sha,
            result_sha256=result_sha256,
            file=file,
            session=session,
            detail=detail or {},
            git_head=git_head,
        )
        manifest.entries.append(entry)
        return entry


def snapshot_write_chapter(
    project: WritingProject,
    number: int,
    fm: dict[str, Any],
    body: str,
    reason: str,
    session: str = "",
    detail: dict[str, Any] | None = None,
) -> Path:
    """Write a chapter through the snapshot chokepoint.

    Four-step sequence: detect human-edit drift (snapshot it first), snapshot
    the prior body under `reason`, call `WritingProject.write_chapter`, then
    record the written body's hash on the manifest so the next write can
    detect drift. Appends a `drafts.snapshot` ledger entry per snapshot
    recorded. With `archaeology.enabled: false` this is a straight
    pass-through to `write_chapter`.
    """
    if not project.config.archaeology.enabled:
        return project.write_chapter(number, fm, body)

    store = DraftStore(project)
    manifest = store.load_manifest(number)
    ledger = Ledger(project.root)
    target = project.chapter_rel(number)
    git_head = _git_head(project.root)

    prior_text: str | None
    try:
        prior_text = project.read(target)
    except ProjectError:
        prior_text = None

    entry: SnapshotEntry | None = None
    if prior_text is not None:
        _prior_fm, prior_body = split_frontmatter(prior_text)
        prior_sha = body_hash(prior_body)
        if manifest.last_result_sha256 and prior_sha != manifest.last_result_sha256:
            # Hand-edited since the last harness write: preserve it first.
            drift = store.record(
                number,
                manifest,
                prior_text,
                prior_sha,
                "human-edit",
                session=session,
                git_head=git_head,
                result_sha256=prior_sha,
            )
            ledger.append(
                "drafts.snapshot",
                target=target,
                session=session,
                chapter=number,
                seq=drift.seq,
                reason="human-edit",
            )
        # If the write leaves the body unchanged (frontmatter-only churn),
        # no file is needed: the content stays current on disk.
        new_sha_estimate = body_hash(body.lstrip("\n"))
        entry = store.record(
            number,
            manifest,
            prior_text,
            prior_sha,
            reason,
            session=session,
            detail=detail,
            git_head=git_head,
            skip_file=prior_sha == new_sha_estimate,
        )

    path = project.write_chapter(number, fm, body)
    _new_fm, new_body = project.read_chapter(number)
    new_sha = body_hash(new_body)
    manifest.last_result_sha256 = new_sha
    if entry is not None:
        entry.result_sha256 = new_sha
        ledger.append(
            "drafts.snapshot",
            target=target,
            session=session,
            chapter=number,
            seq=entry.seq,
            reason=reason,
        )
    store.save_manifest(number, manifest)
    return path
