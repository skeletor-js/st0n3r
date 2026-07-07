"""Voice fingerprint: learn, persist, and summarize the measured voice.

A fingerprint is per-feature mean and dispersion over ~1000-word exemplar
segments (see :mod:`stoner.voice.features`) -- dispersion is what turns
"normal variation for this writer" into a measurement drift can be scored
against. It is derived state under ``.stoner/voice/fingerprint.json``
(never in ``canon/``), versioned so a schema change fails loudly with a
"re-run ``stoner voice learn``" error instead of scoring garbage.

Learning is deterministic: identical exemplar texts always produce
identical feature stats. Exemplars are local files read from disk only;
nothing here touches the network.
"""

from __future__ import annotations

import json
import statistics
import time
from pathlib import Path

from pydantic import BaseModel, Field, ValidationError

from ..project import WritingProject, count_words, split_frontmatter
from ..slop.analyzers import mask_code_fences
from .features import extract_features, segment_text

FINGERPRINT_VERSION = 1

#: Project-relative location of the fingerprint state file.
FINGERPRINT_REL = ".stoner/voice/fingerprint.json"

#: Below this many exemplar words, `learn` refuses -- the stats would be
#: noise. Between the floor and the comfort threshold, the fingerprint is
#: flagged thin and drift scoring damps its verdicts.
HARD_FLOOR_WORDS = 1000
COMFORT_WORDS = 5000

#: Character cap for the plain-text digest handed to prompts.
DIGEST_MAX_CHARS = 1200


class FingerprintError(RuntimeError):
    """Missing, corrupt, undersized, or version-mismatched fingerprint."""


class FeatureStat(BaseModel):
    """Mean and population standard deviation of one feature over segments."""

    mean: float
    std: float


class Fingerprint(BaseModel):
    """The measured voice: per-feature stats plus corpus provenance."""

    version: int = FINGERPRINT_VERSION
    features: dict[str, FeatureStat] = Field(default_factory=dict)
    exemplars: list[str] = Field(default_factory=list)  # source labels
    segment_count: int = 0
    total_words: int = 0
    thin: bool = False  # corpus below COMFORT_WORDS; drift damps scores
    created_at: float = Field(default_factory=time.time)


def learn_fingerprint(texts: list[tuple[str, str]]) -> Fingerprint:
    """Learn a fingerprint from ``(label, raw_text)`` exemplar pairs.

    Each text is frontmatter-stripped and code-fence-masked, segmented into
    ~1000-word chunks on paragraph boundaries, and measured; the fingerprint
    is the per-feature mean/std across all segments. Raises
    :class:`FingerprintError` below :data:`HARD_FLOOR_WORDS`.
    """
    segments: list[str] = []
    labels: list[str] = []
    for label, text in texts:
        _fm, body = split_frontmatter(text)
        masked = mask_code_fences(body)
        segs = segment_text(masked)
        if segs:
            labels.append(label)
            segments.extend(segs)
    total_words = sum(count_words(s) for s in segments)
    if total_words < HARD_FLOOR_WORDS:
        raise FingerprintError(
            f"Exemplar corpus is only {total_words} words -- at least "
            f"{HARD_FLOOR_WORDS} are needed to learn a fingerprint. Add more "
            "prose to notes/exemplars/ (or pass more files) and re-run "
            "`stoner voice learn`."
        )
    vectors = [extract_features(seg) for seg in segments]
    features: dict[str, FeatureStat] = {}
    for name in sorted(vectors[0]):
        values = [v[name] for v in vectors]
        features[name] = FeatureStat(mean=statistics.mean(values), std=statistics.pstdev(values))
    return Fingerprint(
        features=features,
        exemplars=labels,
        segment_count=len(segments),
        total_words=total_words,
        thin=total_words < COMFORT_WORDS,
    )


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


def fingerprint_path(project: WritingProject) -> Path:
    return project.root / ".stoner" / "voice" / "fingerprint.json"


def save_fingerprint(project: WritingProject, fp: Fingerprint) -> Path:
    path = fingerprint_path(project)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(fp.model_dump_json(indent=2), encoding="utf-8")
    return path


def load_fingerprint(project: WritingProject) -> Fingerprint:
    """Load the saved fingerprint; every failure mode names the fix."""
    path = fingerprint_path(project)
    if not path.exists():
        raise FingerprintError(
            "No voice fingerprint learned yet. Put exemplar prose in "
            "notes/exemplars/ and run `stoner voice learn`."
        )
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise FingerprintError(
            f"{FINGERPRINT_REL} is not valid JSON ({exc}). Re-run "
            "`stoner voice learn` to rebuild it."
        ) from exc
    if not isinstance(data, dict):
        raise FingerprintError(
            f"{FINGERPRINT_REL} does not contain a fingerprint object. "
            "Re-run `stoner voice learn` to rebuild it."
        )
    version = data.get("version")
    if version != FINGERPRINT_VERSION:
        raise FingerprintError(
            f"Fingerprint version {version!r} does not match this st0n3r "
            f"(expected {FINGERPRINT_VERSION}). Re-run `stoner voice learn`."
        )
    try:
        return Fingerprint.model_validate(data)
    except ValidationError as exc:
        raise FingerprintError(
            f"{FINGERPRINT_REL} is malformed ({exc.error_count()} schema "
            "error(s)). Re-run `stoner voice learn` to rebuild it."
        ) from exc


# ---------------------------------------------------------------------------
# Digest (for prompts and `stoner voice show`)
# ---------------------------------------------------------------------------


def _fmt(value: float) -> str:
    return f"{value:.2f}".rstrip("0").rstrip(".")


def render_digest(fp: Fingerprint) -> str:
    """Short plain-text summary of the fingerprint for prompts/CLI.

    Bounded (<= :data:`DIGEST_MAX_CHARS` chars), no markup beyond simple
    dashes -- it gets embedded verbatim in system prompts.
    """

    def stat(name: str) -> str:
        s = fp.features.get(name)
        if s is None:
            return "n/a"
        return f"{_fmt(s.mean)}±{_fmt(s.std)}"

    top_fw = sorted(
        ((name[3:], s.mean) for name, s in fp.features.items() if name.startswith("fw.")),
        key=lambda kv: kv[1],
        reverse=True,
    )[:8]
    fw_line = ", ".join(f"{w} {_fmt(rate)}" for w, rate in top_fw)
    sources = ", ".join(fp.exemplars[:4]) + (", ..." if len(fp.exemplars) > 4 else "")
    lines = [
        f"Voice fingerprint (v{fp.version}): {fp.segment_count} segment(s), "
        f"{fp.total_words} words from {len(fp.exemplars)} source(s): {sources}"
        + (" [thin corpus -- scores damped]" if fp.thin else ""),
        f"- sentences: {stat('rhythm.sentence_len_mean')} words, "
        f"length CV {stat('rhythm.sentence_len_cv')}, "
        f"short(<=6w) {stat('rhythm.short_sentence_rate')}, "
        f"long(>=30w) {stat('rhythm.long_sentence_rate')}",
        f"- punctuation: commas/sentence {stat('punctuation.commas_per_sentence')}; "
        f"per 1000 words: em-dash {stat('punctuation.em_dash_per_1000')}, "
        f"semicolon {stat('punctuation.semicolon_per_1000')}, "
        f"ellipsis {stat('punctuation.ellipsis_per_1000')}, "
        f"question {stat('punctuation.question_per_1000')}, "
        f"exclamation {stat('punctuation.exclamation_per_1000')}",
        f"- register: word length {stat('register.word_len_mean')}, "
        f"latinate/1000 {stat('register.latinate_per_1000')}, "
        f"windowed TTR {stat('register.ttr_windowed')}, "
        f"contractions/1000 {stat('register.contraction_per_1000')}",
        f"- paragraphs: {stat('paragraph.words_mean')} words, "
        f"{stat('paragraph.sentences_mean')} sentences",
        f"- dialogue: {stat('dialogue.ratio')} of words in quotes, "
        f"run length {stat('dialogue.run_len_mean')} words",
        f"- top function-word rates/1000: {fw_line}",
    ]
    text = "\n".join(lines)
    if len(text) > DIGEST_MAX_CHARS:
        text = text[: DIGEST_MAX_CHARS - 1].rstrip() + "…"
    return text
