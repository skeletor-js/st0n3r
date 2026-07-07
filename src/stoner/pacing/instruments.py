"""Deterministic book-level pacing instruments.

Each instrument is a pure function `(chapters, config) -> InstrumentResult`
over the pre-assembled `list[ChapterData]` (see `pacing/data.py`): findings,
a stats dict, and a per-chapter `series` mapping keyed by chapter number --
the timeline spine the report table and UI overlay render. No instrument
here ever calls a provider or writes a file (hard invariant: the
deterministic layer is free and reproducible).

Findings carry `source="pacing:<instrument>"` and a `ch-NN:<bucket>`
category prefix so `book_review.finding_chapter`-style recovery works.
Heuristics are pragmatic, not perfect -- same stance as the slop analyzers;
thresholds live on :class:`~stoner.config.PacingConfig` where the plan names
them and as module constants otherwise.
"""

from __future__ import annotations

import re
import statistics
from dataclasses import dataclass, field
from typing import Any

from ..config import PacingConfig
from ..types import Finding, Severity, Span
from .data import ChapterData
from .segments import _PAST_PERFECT_RE, _QUOTE_RE, _TEMPORAL_RE, INTERIORITY_VERBS
from .segments import split_paragraphs as _split_paragraphs

# ---------------------------------------------------------------------------
# Shared shapes / constants
# ---------------------------------------------------------------------------

#: Cap on findings emitted per instrument so one systemic problem does not
#: flood the report (all chapters still count toward stats/series).
FINDINGS_CAP_PER_INSTRUMENT = 12

#: Length trajectory: a strictly monotonic run of at least this many chapters
#: whose total change is at least MONOTONIC_MIN_CHANGE of the run's start
#: flags drift.
MONOTONIC_MIN_RUN = 5
MONOTONIC_MIN_CHANGE = 0.15
#: Dead-uniform lengths: CV below this over MIN_CHAPTERS_FOR_CV+ chapters.
LENGTH_CV_UNIFORM = 0.08
MIN_CHAPTERS_FOR_CV = 5
#: Outliers: deviation from the median beyond both bounds below.
OUTLIER_MAD_FACTOR = 3.0
OUTLIER_MIN_FRACTION_OF_MEDIAN = 0.4
MIN_CHAPTERS_FOR_OUTLIERS = 4

#: Ending-shape taxonomy (fixed five shapes; not a config surface).
ENDING_SHAPES = (
    "one_line_punch",
    "dialogue_close",
    "question_close",
    "summary_close",
    "cliff_close",
)
#: A final paragraph at or under this many words reads as a one-line punch.
PUNCH_MAX_WORDS = 12
#: Final-paragraph quoted coverage at or above this is a dialogue close.
DIALOGUE_CLOSE_MIN_COVERAGE = 0.4

#: Beat-sheet sections that must not be empty (from the beats template).
REQUIRED_BEAT_SECTIONS = ("Goal", "Conflict", "Turn", "Exit State")

_SECTION_RE = re.compile(r"^##\s+(.+?)\s*$", re.MULTILINE)
_HTML_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
_WORD_RE = re.compile(r"\b[\w''-]+\b")

#: Quote length cap for findings that quote a summary block.
_QUOTE_CAP = 200


@dataclass
class InstrumentResult:
    """One instrument's output: findings + stats + the per-chapter series."""

    findings: list[Finding] = field(default_factory=list)
    stats: dict[str, Any] = field(default_factory=dict)
    series: dict[int, Any] = field(default_factory=dict)


def _finding(
    instrument: str,
    severity: Severity,
    chapter: int,
    bucket: str,
    issue: str,
    *,
    quote: str = "",
    span: Span | None = None,
    suggestion: str = "",
) -> Finding:
    return Finding(
        source=f"pacing:{instrument}",
        severity=severity,
        category=f"ch-{chapter:02d}:{bucket}",
        quote=quote,
        span=span,
        issue=issue,
        suggestion=suggestion,
    )


def _runs(values: list[Any]) -> list[tuple[int, int, Any]]:
    """Maximal runs of equal consecutive values as (start_idx, end_idx, value)."""
    out: list[tuple[int, int, Any]] = []
    if not values:
        return out
    start = 0
    for i in range(1, len(values) + 1):
        if i == len(values) or values[i] != values[start]:
            out.append((start, i - 1, values[start]))
            start = i
    return out


# ---------------------------------------------------------------------------
# Scene map (R1): in-scene fraction vs. the configured target
# ---------------------------------------------------------------------------


def scene_map(chapters: list[ChapterData], config: PacingConfig) -> InstrumentResult:
    """Flag chapters whose in-scene fraction falls below the target, quoting
    each offender's largest summary block."""
    result = InstrumentResult()
    below = 0
    for ch in chapters:
        frac = ch.segments.in_scene_fraction
        result.series[ch.number] = round(frac, 3)
        if not ch.words or frac >= config.in_scene_min_ratio:
            continue
        below += 1
        severity = Severity.major if frac < config.in_scene_min_ratio / 2 else Severity.minor
        quote = ""
        span = None
        worst = ch.segments.largest_summary_span()
        if worst is not None:
            s, e = worst
            quote = ch.body[s : min(e, s + _QUOTE_CAP)]
            span = Span(start=s, end=e, line=ch.body.count("\n", 0, s) + 1)
        if len(result.findings) < FINDINGS_CAP_PER_INSTRUMENT:
            result.findings.append(
                _finding(
                    "scene_map", severity, ch.number, "summary_heavy",
                    f"chapter {ch.number} is {frac:.0%} in-scene "
                    f"(target {config.in_scene_min_ratio:.0%}) -- summary is doing scene work",
                    quote=quote, span=span,
                    suggestion="dramatize the largest summarized stretch as a scene, or cut it",
                )
            )
    result.stats = {
        "chapters_below_target": below,
        "target": config.in_scene_min_ratio,
        "mean_in_scene_fraction": (
            round(statistics.mean(ch.segments.in_scene_fraction for ch in chapters), 3)
            if chapters else 0.0
        ),
    }
    return result


# ---------------------------------------------------------------------------
# POV cadence (R3)
# ---------------------------------------------------------------------------


def pov_cadence(chapters: list[ChapterData], config: PacingConfig) -> InstrumentResult:
    """POV sequence from frontmatter: long single-POV runs in a multi-POV
    book, and POVs that appear once then vanish."""
    result = InstrumentResult()
    seq = [(ch.pov or "unknown") for ch in chapters]
    for ch, pov in zip(chapters, seq, strict=True):
        result.series[ch.number] = pov
    distinct = sorted(set(seq))
    runs = _runs(seq)

    if len(distinct) > 1:
        for start, end, pov in runs:
            run_len = end - start + 1
            if run_len < config.pov_break_min_run:
                continue
            first, last = chapters[start].number, chapters[end].number
            if len(result.findings) < FINDINGS_CAP_PER_INSTRUMENT:
                result.findings.append(
                    _finding(
                        "pov", Severity.minor, first, "cadence_break",
                        f"POV '{pov}' holds chapters {first}-{last} "
                        f"({run_len} in a row) in a book that otherwise rotates POV",
                    )
                )
        # POVs that appear exactly once and never return (not near the end,
        # where "hasn't returned yet" is the honest reading).
        if len(chapters) >= 4:
            for pov in distinct:
                idxs = [i for i, p in enumerate(seq) if p == pov]
                if len(idxs) == 1 and idxs[0] < len(seq) - 2:
                    n = chapters[idxs[0]].number
                    if len(result.findings) < FINDINGS_CAP_PER_INSTRUMENT:
                        result.findings.append(
                            _finding(
                                "pov", Severity.minor, n, "vanishing_pov",
                                f"POV '{pov}' appears only in chapter {n} and never returns",
                            )
                        )

    result.stats = {
        "distinct_povs": distinct,
        "longest_run": max((e - s + 1 for s, e, _ in runs), default=0),
        "unknown_pov_chapters": sum(1 for p in seq if p == "unknown"),
    }
    return result


# ---------------------------------------------------------------------------
# Length trajectory (R4)
# ---------------------------------------------------------------------------


def length_trajectory(chapters: list[ChapterData], config: PacingConfig) -> InstrumentResult:
    """Word-count series: monotonic drift, outliers, dead-uniform stretches."""
    result = InstrumentResult()
    lengths = [ch.words for ch in chapters]
    for ch in chapters:
        result.series[ch.number] = ch.words
    result.stats = {"lengths": lengths}
    if len(lengths) < 2:
        return result

    # -- monotonic drift ------------------------------------------------
    def _flag_monotonic(direction: int, label: str) -> None:
        start = 0
        for i in range(1, len(lengths) + 1):
            ok = i < len(lengths) and (lengths[i] - lengths[i - 1]) * direction > 0
            if ok:
                continue
            run_len = i - start
            if run_len >= MONOTONIC_MIN_RUN and lengths[start] > 0:
                change = abs(lengths[i - 1] - lengths[start]) / lengths[start]
                if change >= MONOTONIC_MIN_CHANGE:
                    first, last = chapters[start].number, chapters[i - 1].number
                    if len(result.findings) < FINDINGS_CAP_PER_INSTRUMENT:
                        result.findings.append(
                            _finding(
                                "length", Severity.minor, first, f"monotonic_{label}",
                                f"chapter lengths {label} steadily from ch {first} "
                                f"({lengths[start]:,} words) to ch {last} ({lengths[i - 1]:,})",
                            )
                        )
            start = i

    _flag_monotonic(-1, "decline")
    _flag_monotonic(1, "growth")

    # -- outliers ---------------------------------------------------------
    if len(lengths) >= MIN_CHAPTERS_FOR_OUTLIERS:
        med = statistics.median(lengths)
        deviations = [abs(x - med) for x in lengths]
        mad = statistics.median(deviations)
        for ch, dev in zip(chapters, deviations, strict=True):
            if mad > 0 and dev > OUTLIER_MAD_FACTOR * mad and dev > OUTLIER_MIN_FRACTION_OF_MEDIAN * med:
                if len(result.findings) < FINDINGS_CAP_PER_INSTRUMENT:
                    result.findings.append(
                        _finding(
                            "length", Severity.info, ch.number, "outlier",
                            f"chapter {ch.number} is {ch.words:,} words against a "
                            f"median of {med:,.0f} -- a length outlier",
                        )
                    )

    # -- dead-uniform ------------------------------------------------------
    if len(lengths) >= MIN_CHAPTERS_FOR_CV:
        mean = statistics.mean(lengths)
        cv = (statistics.pstdev(lengths) / mean) if mean else 0.0
        result.stats["length_cv"] = round(cv, 4)
        if cv < LENGTH_CV_UNIFORM:
            result.findings.append(
                _finding(
                    "length", Severity.minor, chapters[0].number, "dead_uniform",
                    f"chapter lengths are dead-uniform (CV={cv:.2f} over "
                    f"{len(lengths)} chapters) -- structure landing exactly on schedule",
                )
            )
    return result


# ---------------------------------------------------------------------------
# Ending-shape echo (R5)
# ---------------------------------------------------------------------------


def classify_ending(body: str) -> str:
    """Classify a chapter's final paragraph into one of ENDING_SHAPES.

    Deterministic and total: order of checks is question > dialogue >
    one-line punch > summary/reflection > cliff (the action default).
    """
    spans = _split_paragraphs(body)
    if not spans:
        return "one_line_punch"
    s, e = spans[-1]
    para = body[s:e]
    words = len(_WORD_RE.findall(para))

    stripped = para.rstrip("\"'’”)]* ")
    if stripped.endswith("?"):
        return "question_close"
    quoted = sum(m.end() - m.start() for m in _QUOTE_RE.finditer(para))
    if quoted / max(len(para), 1) >= DIALOGUE_CLOSE_MIN_COVERAGE:
        return "dialogue_close"
    if words <= PUNCH_MAX_WORDS:
        return "one_line_punch"
    interiority = sum(
        1 for m in _WORD_RE.finditer(para) if m.group(0).lower() in INTERIORITY_VERBS
    )
    if _TEMPORAL_RE.search(para) or len(_PAST_PERFECT_RE.findall(para)) >= 2 or interiority >= 2:
        return "summary_close"
    return "cliff_close"


def ending_echo(chapters: list[ChapterData], config: PacingConfig) -> InstrumentResult:
    """Flag runs of consecutive chapters sharing one ending shape."""
    result = InstrumentResult()
    shapes = [classify_ending(ch.body) for ch in chapters]
    for ch, shape in zip(chapters, shapes, strict=True):
        result.series[ch.number] = shape

    echo_runs: list[tuple[int, int, str]] = []
    for start, end, shape in _runs(shapes):
        run_len = end - start + 1
        if run_len < config.ending_echo_min_run:
            continue
        first, last = chapters[start].number, chapters[end].number
        echo_runs.append((first, last, shape))
        severity = (
            Severity.major
            if run_len == len(chapters) and len(chapters) >= config.ending_echo_min_run
            else Severity.minor
        )
        if len(result.findings) < FINDINGS_CAP_PER_INSTRUMENT:
            result.findings.append(
                _finding(
                    "ending_echo", severity, first, "ending_echo",
                    f"chapters {first}-{last} all end with the same shape "
                    f"('{shape.replace('_', ' ')}') -- repeated chapter-ending structure",
                    suggestion="vary how at least one of these chapters lands",
                )
            )
    result.stats = {
        "shapes": {shape: shapes.count(shape) for shape in ENDING_SHAPES if shape in shapes},
        "runs": [[first, last, shape] for first, last, shape in echo_runs],
    }
    return result


# ---------------------------------------------------------------------------
# Beat coverage diff (R6)
# ---------------------------------------------------------------------------


def _beat_sections(beats_text: str) -> dict[str, str]:
    """Split a beat sheet body into {section title: content} with HTML
    comments stripped (the template ships instructions as comments)."""
    matches = list(_SECTION_RE.finditer(beats_text))
    out: dict[str, str] = {}
    for i, m in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(beats_text)
        content = _HTML_COMMENT_RE.sub("", beats_text[m.end() : end]).strip()
        out[m.group(1).strip()] = content
    return out


def beat_coverage(chapters: list[ChapterData], config: PacingConfig) -> InstrumentResult:
    """The deterministic half of beat drift: file presence, POV agreement,
    and empty required sections. Semantic landed/drifted/missed is the
    judge's job (`pacing/judge.py`)."""
    result = InstrumentResult()
    missing = 0
    for ch in chapters:
        if not ch.has_beats:
            missing += 1
            result.series[ch.number] = "missing"
            if len(result.findings) < FINDINGS_CAP_PER_INSTRUMENT:
                result.findings.append(
                    _finding(
                        "beats", Severity.info, ch.number, "beats_missing",
                        f"no beat sheet at outline/beats/ch-{ch.number:02d}.md",
                    )
                )
            continue
        result.series[ch.number] = "present"

        beats_pov = str(ch.beats_frontmatter.get("pov") or "").strip()
        if beats_pov and ch.pov and beats_pov != ch.pov:
            if len(result.findings) < FINDINGS_CAP_PER_INSTRUMENT:
                result.findings.append(
                    _finding(
                        "beats", Severity.minor, ch.number, "pov_mismatch",
                        f"beat sheet says POV '{beats_pov}' but the chapter "
                        f"frontmatter says '{ch.pov}'",
                    )
                )

        sections = _beat_sections(ch.beats_text)
        for name in REQUIRED_BEAT_SECTIONS:
            if not sections.get(name, "").strip():
                if len(result.findings) < FINDINGS_CAP_PER_INSTRUMENT:
                    result.findings.append(
                        _finding(
                            "beats", Severity.info, ch.number, "empty_section",
                            f"beat sheet section '{name}' is empty for chapter {ch.number}",
                        )
                    )
    result.stats = {"missing_beat_sheets": missing, "chapters": len(chapters)}
    return result


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

INSTRUMENTS = {
    "scene_map": scene_map,
    "pov": pov_cadence,
    "length": length_trajectory,
    "ending_echo": ending_echo,
    "beats": beat_coverage,
}


def run_instruments(
    chapters: list[ChapterData], config: PacingConfig
) -> dict[str, InstrumentResult]:
    """Run every deterministic instrument. Pure: no provider, no writes."""
    return {name: fn(chapters, config) for name, fn in INSTRUMENTS.items()}
