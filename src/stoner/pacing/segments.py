"""Deterministic text segmentation for the pacing instrument layer.

Classifies a chapter body paragraph-by-paragraph into a dialogue /
interiority / action mode mix and an in-scene vs. summary split. Like
`stoner.slop.analyzers.split_sentences`, every heuristic here is pragmatic,
not linguistic: quoted-span coverage stands in for dialogue, thought-verb
density for interiority, past-perfect runs and temporal-compression phrases
for summary narration. Good enough to separate a summary-heavy chapter from
a scene-forward one; not a discourse parser.

Everything is a pure function over the already-frontmatter-stripped,
code-fence-masked body. No provider calls, no file access.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from ..slop.analyzers import TOKEN_RE

# ---------------------------------------------------------------------------
# Heuristic constants
# ---------------------------------------------------------------------------

#: Paragraph split: one or more blank lines.
_PARA_SPLIT_RE = re.compile(r"\n\s*\n")

#: Quoted dialogue span (straight or curly double quotes, single line).
#: Single quotes are deliberately excluded -- apostrophes would swamp them.
_QUOTE_RE = re.compile(r'"[^"\n]+"|“[^”\n]+”')

#: "had <word>" -- a pragmatic past-perfect stand-in (catches "had spent",
#: also "had a plan"; acceptable noise for a summary-tell rate).
_PAST_PERFECT_RE = re.compile(r"\bhad\s+(?:not\s+|never\s+|already\s+|always\s+)?[A-Za-z]+")

#: Temporal-compression phrases that mark narrated time-skips (summary).
TEMPORAL_PHRASES = (
    "over the next",
    "over the following",
    "in the weeks that followed",
    "in the days that followed",
    "in the months that followed",
    "by the time",
    "weeks passed",
    "months passed",
    "days passed",
    "years passed",
    "that summer",
    "that winter",
    "that autumn",
    "that spring",
    "for the next",
    "in the years since",
)
_TEMPORAL_RE = re.compile(
    "|".join(re.escape(p) for p in TEMPORAL_PHRASES), re.IGNORECASE
)

#: Thought/filter verbs marking interiority (slop's FILTER_WORDS spirit plus
#: the wondered/knew/remembered class and free-indirect tells).
INTERIORITY_VERBS = {
    "wondered", "knew", "remembered", "felt", "thought", "realized",
    "realised", "hoped", "feared", "wished", "imagined", "believed",
    "understood", "considered", "recalled", "supposed", "doubted",
    "guessed", "decided", "wanted", "longed", "dreaded", "suspected",
    "meant", "reasoned", "regretted",
}

#: A paragraph with at least this much quoted coverage counts as in-scene
#: regardless of other tells (people talking is a scene).
DIALOGUE_SCENE_MIN_COVERAGE = 0.15
#: Above this quoted coverage the paragraph's *mode* is dialogue.
DIALOGUE_MODE_MIN_COVERAGE = 0.30
#: Summary tell: at least this many "had <verb>" hits ...
SUMMARY_HAD_MIN_COUNT = 2
#: ... at at least this rate per 100 words.
SUMMARY_HAD_MIN_PER_100 = 2.0
#: Interiority mode: at least this many thought verbs ...
INTERIORITY_MIN_COUNT = 2
#: ... at at least this rate per 100 words.
INTERIORITY_MIN_PER_100 = 1.5


# ---------------------------------------------------------------------------
# Result shapes
# ---------------------------------------------------------------------------


@dataclass
class ParagraphSeg:
    """One classified paragraph, in body-local coordinates."""

    start: int
    end: int
    words: int
    mode: str  # "dialogue" | "interiority" | "action"
    is_scene: bool
    dialogue_coverage: float


@dataclass
class SegmentProfile:
    """Per-chapter segmentation summary: mode mix + scene/summary split.

    Ratios are word-weighted fractions in [0, 1]; an empty body yields all
    zeros (never a division error).
    """

    dialogue_ratio: float = 0.0
    interiority_ratio: float = 0.0
    action_ratio: float = 0.0
    in_scene_fraction: float = 0.0
    paragraphs: list[ParagraphSeg] = field(default_factory=list)

    def largest_summary_span(self) -> tuple[int, int] | None:
        """(start, end) of the longest summary-classified paragraph, if any."""
        summaries = [p for p in self.paragraphs if not p.is_scene]
        if not summaries:
            return None
        worst = max(summaries, key=lambda p: p.words)
        return (worst.start, worst.end)


def _clamp(x: float) -> float:
    return max(0.0, min(1.0, x))


def _classify_paragraph(text: str, start: int, end: int) -> ParagraphSeg:
    words = len(TOKEN_RE.findall(text))
    quoted_chars = sum(m.end() - m.start() for m in _QUOTE_RE.finditer(text))
    coverage = _clamp(quoted_chars / max(len(text), 1))

    had_hits = len(_PAST_PERFECT_RE.findall(text))
    had_per_100 = had_hits / max(words, 1) * 100.0
    temporal_hits = len(_TEMPORAL_RE.findall(text))

    interiority_hits = sum(
        1 for m in TOKEN_RE.finditer(text) if m.group(0).lower() in INTERIORITY_VERBS
    )
    interiority_per_100 = interiority_hits / max(words, 1) * 100.0

    if coverage >= DIALOGUE_SCENE_MIN_COVERAGE:
        is_scene = True
    else:
        is_scene = not (
            temporal_hits >= 1
            or (had_hits >= SUMMARY_HAD_MIN_COUNT and had_per_100 >= SUMMARY_HAD_MIN_PER_100)
        )

    if coverage >= DIALOGUE_MODE_MIN_COVERAGE:
        mode = "dialogue"
    elif interiority_hits >= INTERIORITY_MIN_COUNT and interiority_per_100 >= INTERIORITY_MIN_PER_100:
        mode = "interiority"
    else:
        mode = "action"

    return ParagraphSeg(
        start=start, end=end, words=words, mode=mode, is_scene=is_scene,
        dialogue_coverage=coverage,
    )


def split_paragraphs(body: str) -> list[tuple[int, int]]:
    """Split a body into paragraph (start, end) spans on blank lines."""
    spans: list[tuple[int, int]] = []
    pos = 0
    for m in _PARA_SPLIT_RE.finditer(body):
        if pos < m.start():
            spans.append((pos, m.start()))
        pos = m.end()
    if pos < len(body):
        spans.append((pos, len(body)))
    # trim whitespace-only spans
    out: list[tuple[int, int]] = []
    for s, e in spans:
        while s < e and body[s].isspace():
            s += 1
        while e > s and body[e - 1].isspace():
            e -= 1
        if s < e:
            out.append((s, e))
    return out


def segment_chapter(body: str) -> SegmentProfile:
    """Classify every paragraph of `body` and aggregate the mode mix.

    Ratios are weighted by paragraph word count so a one-line dialogue beat
    does not count as much as a page of narration.
    """
    paragraphs = [
        _classify_paragraph(body[s:e], s, e) for s, e in split_paragraphs(body)
    ]
    total_words = sum(p.words for p in paragraphs)
    if total_words == 0:
        return SegmentProfile(paragraphs=paragraphs)

    def _mode_words(mode: str) -> int:
        return sum(p.words for p in paragraphs if p.mode == mode)

    scene_words = sum(p.words for p in paragraphs if p.is_scene)
    return SegmentProfile(
        dialogue_ratio=_clamp(_mode_words("dialogue") / total_words),
        interiority_ratio=_clamp(_mode_words("interiority") / total_words),
        action_ratio=_clamp(_mode_words("action") / total_words),
        in_scene_fraction=_clamp(scene_words / total_words),
        paragraphs=paragraphs,
    )
