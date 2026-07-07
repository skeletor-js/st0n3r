"""ChapterData assembly: the shared per-chapter input record for every
pacing instrument.

One `ChapterData` per manuscript chapter, read via `WritingProject` only:
frontmatter + body (frontmatter stripped, code fences masked), the chapter's
beat sheet (`outline/beats/ch-NN.md`, empty strings when absent), and its
rolling memory summary (`.stoner/memory.json`, empty when absent). The
segmentation profile (mode mix, in-scene fraction) is computed once here so
every downstream instrument stays a pure function over `list[ChapterData]`.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any

from ..canon.memory import Memory
from ..project import ProjectError, WritingProject, count_words, split_frontmatter
from ..slop.analyzers import mask_code_fences
from .segments import SegmentProfile, segment_chapter


@dataclass
class ChapterData:
    """Everything the pacing instruments need to know about one chapter."""

    number: int
    title: str = ""
    pov: str = ""
    status: str = "draft"
    frontmatter: dict[str, Any] = field(default_factory=dict)
    body: str = ""  # frontmatter-stripped, code-fence-masked
    words: int = 0
    beats_text: str = ""  # beat-sheet body ("" when no beat sheet exists)
    beats_frontmatter: dict[str, Any] = field(default_factory=dict)
    has_beats: bool = False
    memory_summary: str = ""  # rolling memory summary ("" when absent)
    segments: SegmentProfile = field(default_factory=SegmentProfile)


def chapter_hash(chapter: ChapterData) -> str:
    """Content hash keying the judge cache: chapter body + beat sheet.

    The beat sheet is included because beat verdicts are part of the cached
    judgment; editing either invalidates the entry.
    """
    h = hashlib.sha256()
    h.update(chapter.body.encode("utf-8"))
    h.update(b"\x00")
    h.update(chapter.beats_text.encode("utf-8"))
    return h.hexdigest()


def assemble_chapters(project: WritingProject) -> list[ChapterData]:
    """Build one `ChapterData` per manuscript chapter, in chapter order.

    Beat sheets and memory summaries degrade to empty strings when missing
    (a chapter with no beat sheet is a beat-coverage finding, not an error).
    """
    memory = Memory(project)
    out: list[ChapterData] = []
    for info in project.chapters():
        try:
            fm, raw_body = project.read_chapter(info.number)
        except ProjectError:
            continue
        body = mask_code_fences(raw_body)

        beats_text = ""
        beats_fm: dict[str, Any] = {}
        has_beats = False
        try:
            beats_raw = project.read(f"outline/beats/ch-{info.number:02d}.md")
            beats_fm, beats_text = split_frontmatter(beats_raw)
            has_beats = True
        except ProjectError:
            pass

        out.append(
            ChapterData(
                number=info.number,
                title=info.title,
                pov=info.pov,
                status=info.status,
                frontmatter=fm,
                body=body,
                words=count_words(body),
                beats_text=beats_text,
                beats_frontmatter=beats_fm,
                has_beats=has_beats,
                memory_summary=memory.get_chapter_summary(info.number) or "",
                segments=segment_chapter(body),
            )
        )
    return out
