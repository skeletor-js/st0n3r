"""Tests for pacing text segmentation (U1): mode mix, scene/summary split,
and ChapterData assembly. All deterministic, no provider."""

from __future__ import annotations

from pathlib import Path

import pytest

from stoner.canon.memory import Memory
from stoner.canon.scaffold import scaffold_project
from stoner.pacing.data import assemble_chapters, chapter_hash
from stoner.pacing.segments import segment_chapter, split_paragraphs
from stoner.project import WritingProject

# -- paired fixtures ---------------------------------------------------------

SCENE_BLOCK = (
    '"Where is it?" Mara asked, blocking the doorway.\n\n'
    '"Gone," Holt said. "Someone took it in the night. I checked the strongbox '
    'twice before I came to you."\n\n'
    'She grabbed his collar and shoved him back against the shelves. Jars '
    'rattled. "Then you check it a third time."\n'
)

SUMMARY_BLOCK = (
    "She had spent the summer sorting the archive. By the time the rains came "
    "she had catalogued every ledger and had memorized the watermarks. Over the "
    "next few weeks she had grown tired of the work, and in the weeks that "
    "followed she had written to her brother twice without an answer.\n\n"
    "The estate had changed hands three times before her arrival. Each owner "
    "had left less behind than the last, and by the time the bank had claimed "
    "it there had been almost nothing worth keeping.\n"
)

MIXED_BLOCK = SCENE_BLOCK + "\n" + SUMMARY_BLOCK


def test_dialogue_heavy_block_is_in_scene_with_high_dialogue_ratio():
    profile = segment_chapter(SCENE_BLOCK)
    assert profile.in_scene_fraction > 0.9
    assert profile.dialogue_ratio > 0.4


def test_past_perfect_narration_classifies_as_summary():
    profile = segment_chapter(SUMMARY_BLOCK)
    assert profile.in_scene_fraction < 0.2
    assert all(not p.is_scene for p in profile.paragraphs)


def test_mixed_chapter_lands_between_pure_fixtures():
    scene = segment_chapter(SCENE_BLOCK).in_scene_fraction
    summary = segment_chapter(SUMMARY_BLOCK).in_scene_fraction
    mixed = segment_chapter(MIXED_BLOCK).in_scene_fraction
    assert summary < mixed < scene


def test_empty_body_does_not_divide_by_zero():
    profile = segment_chapter("")
    assert profile.in_scene_fraction == 0.0
    assert profile.dialogue_ratio == 0.0
    assert profile.paragraphs == []


def test_dialogue_only_body_ratios_clamp_to_unit_interval():
    body = '"Yes." "No." "Maybe." "We go at dawn," she said.\n'
    profile = segment_chapter(body)
    for value in (
        profile.dialogue_ratio,
        profile.interiority_ratio,
        profile.action_ratio,
        profile.in_scene_fraction,
    ):
        assert 0.0 <= value <= 1.0
    assert profile.dialogue_ratio > 0.4


def test_mode_ratios_sum_to_one_for_nonempty_body():
    profile = segment_chapter(MIXED_BLOCK)
    total = profile.dialogue_ratio + profile.interiority_ratio + profile.action_ratio
    assert total == pytest.approx(1.0)


def test_largest_summary_span_points_at_summary_text():
    profile = segment_chapter(MIXED_BLOCK)
    span = profile.largest_summary_span()
    assert span is not None
    s, e = span
    assert "had" in MIXED_BLOCK[s:e]


def test_split_paragraphs_spans_are_trimmed_slices():
    for s, e in split_paragraphs(MIXED_BLOCK):
        chunk = MIXED_BLOCK[s:e]
        assert chunk == chunk.strip()
        assert chunk


# -- ChapterData assembly ---------------------------------------------------


@pytest.fixture()
def project(tmp_path: Path) -> WritingProject:
    proj = WritingProject.create(tmp_path / "book", "Test Book")
    scaffold_project(proj, "Test Book")
    return proj


def test_assemble_chapters_one_record_per_chapter(project: WritingProject):
    project.write_chapter(1, {"title": "One", "pov": "Mara"}, SCENE_BLOCK)
    project.write_chapter(2, {"title": "Two", "pov": "Holt"}, SUMMARY_BLOCK)
    Memory(project).set_chapter_summary(1, "Mara confronts Holt over the theft.", pov="Mara")

    chapters = assemble_chapters(project)
    assert [c.number for c in chapters] == [1, 2]

    ch1, ch2 = chapters
    # scaffold ships outline/beats/ch-01.md, so chapter 1 has beat text
    assert ch1.has_beats and ch1.beats_text
    assert ch1.memory_summary == "Mara confronts Holt over the theft."
    assert ch1.pov == "Mara"
    assert ch1.words > 0
    assert ch1.segments.in_scene_fraction > 0.5

    # chapter 2 has neither a beat sheet nor a memory summary
    assert not ch2.has_beats
    assert ch2.beats_text == ""
    assert ch2.memory_summary == ""


def test_assemble_chapters_empty_project_returns_empty_list(project: WritingProject):
    assert assemble_chapters(project) == []


def test_chapter_hash_changes_with_body_and_beats(project: WritingProject):
    project.write_chapter(1, {"title": "One"}, SCENE_BLOCK)
    h1 = chapter_hash(assemble_chapters(project)[0])
    project.write_chapter(1, {"title": "One"}, SCENE_BLOCK + "\nA new line.\n")
    h2 = chapter_hash(assemble_chapters(project)[0])
    assert h1 != h2
    project.write("outline/beats/ch-01.md", "---\nchapter: 1\n---\n\n## Goal\n\nchanged\n")
    h3 = chapter_hash(assemble_chapters(project)[0])
    assert h3 != h2
