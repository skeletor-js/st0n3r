"""Deterministic motif measurement: recurrence matrix, candidate mining,
rhyme overlap.

Everything here is pure string/stem arithmetic -- no model ever runs (that is
`judge.py`). The numbers are reproducible: identical inputs produce
byte-identical reports across runs (hard invariant 2), so numeric output is
allowed here where it is forbidden in the advisory LLM layer.

Tokenization and the stopword list are reused from `slop/analyzers.py`
(`iter_tokens`, `STOPWORDS`) -- both are stable, pure helpers -- plus a crude
module-local suffix stemmer so an inflected anchor ("rivers") still matches
its registered form ("river"). Matching is stem-normalized phrase containment:
never semantic, never embedding-based (deliberately deferred).
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from ..canon.store import CanonStore
from ..project import WritingProject
from ..slop.analyzers import STOPWORDS, iter_tokens

# ---------------------------------------------------------------------------
# Thresholds / stemmer
# ---------------------------------------------------------------------------

#: n-gram sizes mined for candidate motifs (mirrors slop's NGRAM_SIZES).
_NGRAM_SIZES = (3, 4)
#: A word this length or shorter is never stemmed (avoids mangling short words).
_STEM_MIN_LEN = 4
#: The stemmed remainder must keep at least this many characters.
_STEM_FLOOR = 3
#: Suffixes stripped, longest first, so "-ing" wins over "-s".
_SUFFIXES = ("ing", "ed", "es", "s")


def stem(word: str) -> str:
    """Crude suffix stemmer: strip one of -ing/-ed/-es/-s past a length floor.

    Deliberately not a real stemmer -- just enough that "rivers" and "river"
    collapse to the same key for anchor matching. Short words pass through.
    """
    w = word.lower()
    if len(w) <= _STEM_MIN_LEN:
        return w
    for suffix in _SUFFIXES:
        if w.endswith(suffix) and len(w) - len(suffix) >= _STEM_FLOOR:
            return w[: -len(suffix)]
    return w


def _stems(text: str) -> list[str]:
    """Stemmed token stream for a body of text (lowercased, ASCII words)."""
    return [stem(w) for w, _ in iter_tokens(text)]


def _phrase_stems(phrase: str) -> list[str]:
    return [stem(w) for w, _ in iter_tokens(phrase)]


def _count_phrase(tokens: list[str], phrase: list[str]) -> int:
    """Count non-overlapping-free (sliding) occurrences of `phrase` in `tokens`."""
    n = len(phrase)
    if n == 0 or n > len(tokens):
        return 0
    return sum(1 for i in range(len(tokens) - n + 1) if tokens[i : i + n] == phrase)


def _is_sublist(needle: list[str], haystack: list[str]) -> bool:
    n = len(needle)
    if n == 0 or n > len(haystack):
        return False
    return any(haystack[i : i + n] == needle for i in range(len(haystack) - n + 1))


# ---------------------------------------------------------------------------
# Chapter access
# ---------------------------------------------------------------------------


def _chapter_bodies(project: WritingProject) -> list[tuple[int, str]]:
    """(chapter_number, frontmatter-stripped body) for every manuscript
    chapter, ascending. `read_chapter` already strips frontmatter."""
    out: list[tuple[int, str]] = []
    for info in project.chapters():
        try:
            _, body = project.read_chapter(info.number)
        except Exception:  # noqa: BLE001 - a broken chapter file is skipped
            continue
        out.append((info.number, body))
    return out


# ---------------------------------------------------------------------------
# Report shapes (feature-local dataclasses)
# ---------------------------------------------------------------------------


@dataclass
class MotifScanRow:
    motif_id: str
    motif: str
    per_chapter: dict[int, int] = field(default_factory=dict)
    chapters_hit: int = 0
    first: int | None = None
    last: int | None = None


@dataclass
class MotifScanReport:
    rows: list[MotifScanRow] = field(default_factory=list)
    chapters: list[int] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    json_path: str = ""


@dataclass
class CandidateRow:
    gram: str
    chapters: list[int] = field(default_factory=list)
    total: int = 0


@dataclass
class CandidateReport:
    candidates: list[CandidateRow] = field(default_factory=list)
    min_chapters: int = 3
    cap: int = 12
    created_at: float = field(default_factory=time.time)


@dataclass
class RhymeReport:
    jaccard: float = 0.0
    shared_distinctive: list[str] = field(default_factory=list)
    motifs_both: list[str] = field(default_factory=list)
    motifs_open_only: list[str] = field(default_factory=list)
    motifs_close_only: list[str] = field(default_factory=list)
    opening_chapters: list[int] = field(default_factory=list)
    closing_chapters: list[int] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    json_path: str = ""


# ---------------------------------------------------------------------------
# Recurrence matrix
# ---------------------------------------------------------------------------


def scan_motifs(project: WritingProject, store: CanonStore) -> MotifScanReport:
    """Map every registered motif across all chapters by stemmed-anchor
    phrase containment. Empty registry yields an empty matrix, no error."""
    bodies = _chapter_bodies(project)
    chapter_nums = [n for n, _ in bodies]
    stem_by_chapter = {n: _stems(body) for n, body in bodies}

    rows: list[MotifScanRow] = []
    for motif in store.motifs():
        anchor_stems = [_phrase_stems(a) for a in motif.anchor_list()]
        anchor_stems = [a for a in anchor_stems if a]
        per_chapter: dict[int, int] = {}
        for n in chapter_nums:
            toks = stem_by_chapter[n]
            count = sum(_count_phrase(toks, a) for a in anchor_stems)
            if count:
                per_chapter[n] = count
        hits = sorted(per_chapter)
        rows.append(
            MotifScanRow(
                motif_id=motif.id,
                motif=motif.motif,
                per_chapter=per_chapter,
                chapters_hit=len(hits),
                first=hits[0] if hits else None,
                last=hits[-1] if hits else None,
            )
        )
    return MotifScanReport(rows=rows, chapters=chapter_nums)


# ---------------------------------------------------------------------------
# Candidate mining
# ---------------------------------------------------------------------------


def _anchor_stem_lists(store: CanonStore) -> list[list[str]]:
    out: list[list[str]] = []
    for motif in store.motifs():
        for anchor in motif.anchor_list():
            s = _phrase_stems(anchor)
            if s:
                out.append(s)
    return out


def _covered(gram_stems: list[str], anchors: list[list[str]]) -> bool:
    """True if a registered anchor covers this gram: either the anchor's
    stems are a contiguous sublist of the gram or vice versa."""
    return any(
        _is_sublist(anchor, gram_stems) or _is_sublist(gram_stems, anchor)
        for anchor in anchors
    )


def mine_candidates(
    project: WritingProject,
    store: CanonStore,
    min_chapters: int = 3,
    cap: int = 12,
) -> CandidateReport:
    """Mine unregistered content n-grams recurring across >= `min_chapters`
    distinct chapters, excluding grams covered by a registered anchor.

    Mirrors `analyze_repetition`'s n-gram loop but aggregates distinct-chapter
    spread instead of per-document occurrences. Returns the top `cap` grams by
    chapter spread (ties broken by total occurrences, then the gram text)."""
    bodies = _chapter_bodies(project)
    anchors = _anchor_stem_lists(store)

    # gram -> {chapters: set[int], total: int}
    chapters_by_gram: dict[str, set[int]] = {}
    total_by_gram: dict[str, int] = {}
    for n_ch, body in bodies:
        words = [w for w, _ in iter_tokens(body)]
        for size in _NGRAM_SIZES:
            for i in range(len(words) - size + 1):
                gram_words = words[i : i + size]
                if all(w in STOPWORDS for w in gram_words):
                    continue
                key = " ".join(gram_words)
                chapters_by_gram.setdefault(key, set()).add(n_ch)
                total_by_gram[key] = total_by_gram.get(key, 0) + 1

    candidates: list[CandidateRow] = []
    for gram, chapters in chapters_by_gram.items():
        if len(chapters) < min_chapters:
            continue
        gram_stems = [stem(w) for w in gram.split()]
        if _covered(gram_stems, anchors):
            continue
        candidates.append(
            CandidateRow(gram=gram, chapters=sorted(chapters), total=total_by_gram[gram])
        )

    candidates.sort(key=lambda c: (-len(c.chapters), -c.total, c.gram))
    return CandidateReport(
        candidates=candidates[:cap], min_chapters=min_chapters, cap=cap
    )


# ---------------------------------------------------------------------------
# Rhyme: opening vs closing overlap
# ---------------------------------------------------------------------------


def window_bodies(
    project: WritingProject, window: int = 1
) -> tuple[list[int], list[int], list[int], str, str, str]:
    """Split chapters into opening/closing/middle windows.

    Returns (all_nums, opening_nums, closing_nums, opening_text, closing_text,
    middle_text). Opening = first `window` chapters, closing = last `window`;
    when the manuscript is too short to separate them the windows may overlap.
    """
    bodies = _chapter_bodies(project)
    nums = [n for n, _ in bodies]
    body_by_num = dict(bodies)
    opening_nums = nums[:window]
    closing_nums = nums[-window:] if nums else []
    middle_nums = [n for n in nums if n not in set(opening_nums) | set(closing_nums)]
    opening_text = "\n\n".join(body_by_num[n] for n in opening_nums)
    closing_text = "\n\n".join(body_by_num[n] for n in closing_nums)
    middle_text = "\n\n".join(body_by_num[n] for n in middle_nums)
    return nums, opening_nums, closing_nums, opening_text, closing_text, middle_text


def _content_stems(text: str) -> set[str]:
    return {s for s in _stems(text) if s not in STOPWORDS and len(s) >= _STEM_FLOOR}


def _motif_hits(store: CanonStore, text: str) -> set[str]:
    """Motif names whose any anchor occurs (stemmed) in `text`."""
    toks = _stems(text)
    hit: set[str] = set()
    for motif in store.motifs():
        for anchor in motif.anchor_list():
            if _count_phrase(toks, _phrase_stems(anchor)):
                hit.add(motif.motif)
                break
    return hit


def rhyme_overlap(
    project: WritingProject, store: CanonStore, window: int = 1
) -> RhymeReport:
    """Deterministic opening/closing overlap: content-token Jaccard,
    distinctive shared terms (in both windows, absent from the middle), and
    motif co-presence buckets. Pure arithmetic -- no verdict."""
    nums, open_nums, close_nums, open_text, close_text, mid_text = window_bodies(
        project, window
    )
    open_set = _content_stems(open_text)
    close_set = _content_stems(close_text)
    mid_set = _content_stems(mid_text)

    union = open_set | close_set
    inter = open_set & close_set
    jaccard = (len(inter) / len(union)) if union else 0.0
    shared_distinctive = sorted(t for t in inter if t not in mid_set)

    open_motifs = _motif_hits(store, open_text)
    close_motifs = _motif_hits(store, close_text)
    return RhymeReport(
        jaccard=round(jaccard, 4),
        shared_distinctive=shared_distinctive,
        motifs_both=sorted(open_motifs & close_motifs),
        motifs_open_only=sorted(open_motifs - close_motifs),
        motifs_close_only=sorted(close_motifs - open_motifs),
        opening_chapters=open_nums,
        closing_chapters=close_nums,
    )
