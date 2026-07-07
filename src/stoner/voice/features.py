"""Deterministic stylometric feature extraction for the voice engine.

Everything here is pure arithmetic over raw prose -- no LLM, no network, no
new dependencies (invariant: numeric voice scores must be computed
deterministically). Callers hand in text that is already
frontmatter-stripped and code-fence-masked (reuse
:func:`stoner.project.split_frontmatter` and
:func:`stoner.slop.analyzers.mask_code_fences` so voice and slop agree on
coordinates and masking); this module only measures.

The output of :func:`extract_features` is a flat ``{name: value}`` mapping.
Feature names are prefixed with their bucket (``rhythm.``, ``paragraph.``,
``punctuation.``, ``fw.``, ``register.``, ``dialogue.``); the fingerprint
(:mod:`stoner.voice.fingerprint`) stores per-feature mean/std over exemplar
segments and drift scoring (:mod:`stoner.voice.drift`) compares against
those. Buckets in :data:`WINDOW_STABLE_BUCKETS` are meaningful on
paragraph-window slices; the rest are chapter-level only.
"""

from __future__ import annotations

import re
import statistics
from collections import Counter
from pathlib import Path

import yaml

from ..slop.analyzers import TOKEN_RE, split_sentences

DATA_DIR = Path(__file__).parent / "data"

#: Exemplar prose is measured in chunks of roughly this many words so the
#: fingerprint captures dispersion ("normal variation for this writer"),
#: not just whole-corpus point estimates.
SEGMENT_TARGET_WORDS = 1000

#: A sentence at or below this word count counts as "short" for the
#: short-sentence rate; at or above the long threshold counts as "long".
SHORT_SENTENCE_WORDS = 6
LONG_SENTENCE_WORDS = 30

#: Window size (in tokens) for the windowed type-token ratio. TTR is
#: length-sensitive, so it is averaged over fixed-size windows.
TTR_WINDOW_TOKENS = 200

#: Feature-name prefix -> bucket name. ``fw.`` is the per-word function-word
#: profile scored as a composite Burrows'-Delta-style distance in drift.py.
BUCKET_BY_PREFIX: dict[str, str] = {
    "rhythm": "rhythm",
    "paragraph": "paragraph",
    "punctuation": "punctuation",
    "fw": "function_words",
    "register": "register",
    "dialogue": "dialogue",
}

#: Buckets whose features are stable enough to score on ~120-word paragraph
#: windows. paragraph/dialogue shape needs a whole chapter to mean anything.
WINDOW_STABLE_BUCKETS: tuple[str, ...] = ("rhythm", "punctuation", "function_words", "register")

#: Latinate derivational suffixes -- the same morphology-not-POS spirit as
#: slop's ``_ADJ_SUFFIXES``. A crude but deterministic register proxy.
_LATINATE_SUFFIX_RE = re.compile(
    r"(?:tion|sion|ment|ance|ence|ity|ous|ive|al|ic|ate|ify|ize|ise|ology|itude|escent)$"
)
#: Words shorter than this never count as latinate (suffix would be most of
#: the word: "ion", "ally", ...).
_LATINATE_MIN_LEN = 6

_PARA_SPLIT_RE = re.compile(r"\n\s*\n")

#: Dialogue: a quote-delimited run, tolerant of straight and curly quotes.
_DIALOGUE_RE = re.compile(r"\"[^\"\n]+\"|“[^”\n]+”")

_EM_DASH_RE = re.compile(r"—|--(?!-)")
_ELLIPSIS_RE = re.compile(r"\.\.\.|…")


class FunctionWordError(ValueError):
    """Raised when data/function_words.yaml is malformed."""


def load_function_words(path: Path | None = None, *, force_reload: bool = False) -> tuple[str, ...]:
    """Load (and cache) the closed-class function-word list.

    Pass ``force_reload=True`` in tests that monkeypatch the data path.
    """
    global _cache
    if _cache is not None and not force_reload and path is None:
        return _cache
    p = path or (DATA_DIR / "function_words.yaml")
    if not p.exists():
        raise FunctionWordError(f"missing function-word file: {p}")
    raw = yaml.safe_load(p.read_text(encoding="utf-8")) or []
    if not isinstance(raw, list):
        raise FunctionWordError(f"{p} must contain a YAML list of words")
    words: list[str] = []
    for item in raw:
        if not isinstance(item, str) or not item.strip():
            raise FunctionWordError(f"{p} has a non-string entry: {item!r}")
        words.append(item.strip().lower())
    loaded = tuple(sorted(set(words)))
    if path is None:
        _cache = loaded
    return loaded


_cache: tuple[str, ...] | None = None


def bucket_of(feature: str) -> str:
    """Bucket name for a feature id like ``rhythm.sentence_len_mean``."""
    prefix = feature.split(".", 1)[0]
    return BUCKET_BY_PREFIX.get(prefix, prefix)


# ---------------------------------------------------------------------------
# Segmentation
# ---------------------------------------------------------------------------


def segment_text(text: str, target_words: int = SEGMENT_TARGET_WORDS) -> list[str]:
    """Split prose into ~``target_words`` chunks on paragraph boundaries.

    Paragraphs are never split. A trailing chunk shorter than half the
    target is merged into the previous one so no segment is degenerate;
    text shorter than the target comes back as one segment.
    """
    paragraphs = [p for p in _PARA_SPLIT_RE.split(text) if p.strip()]
    if not paragraphs:
        return []
    segments: list[list[str]] = []
    current: list[str] = []
    current_words = 0
    for para in paragraphs:
        current.append(para)
        current_words += len(TOKEN_RE.findall(para))
        if current_words >= target_words:
            segments.append(current)
            current, current_words = [], 0
    if current:
        tail_words = sum(len(TOKEN_RE.findall(p)) for p in current)
        if segments and tail_words < target_words // 2:
            segments[-1].extend(current)
        else:
            segments.append(current)
    return ["\n\n".join(seg) for seg in segments]


# ---------------------------------------------------------------------------
# Per-bucket feature functions
# ---------------------------------------------------------------------------


def _rate_per_1000(count: int, word_count: int) -> float:
    return (count / max(word_count, 1)) * 1000.0


def _mean_cv(values: list[int]) -> tuple[float, float]:
    if not values:
        return 0.0, 0.0
    mean = statistics.mean(values)
    if mean == 0:
        return 0.0, 0.0
    return float(mean), statistics.pstdev(values) / mean


def _rhythm_features(text: str) -> dict[str, float]:
    sentences = split_sentences(text)
    lengths = [len(TOKEN_RE.findall(text[s:e])) for s, e in sentences]
    lengths = [n for n in lengths if n > 0]
    mean, cv = _mean_cv(lengths)
    n = max(len(lengths), 1)
    return {
        "rhythm.sentence_len_mean": mean,
        "rhythm.sentence_len_cv": cv,
        "rhythm.short_sentence_rate": sum(1 for x in lengths if x <= SHORT_SENTENCE_WORDS) / n,
        "rhythm.long_sentence_rate": sum(1 for x in lengths if x >= LONG_SENTENCE_WORDS) / n,
    }


def _paragraph_features(text: str) -> dict[str, float]:
    paragraphs = [p for p in _PARA_SPLIT_RE.split(text) if p.strip()]
    word_lengths = [len(TOKEN_RE.findall(p)) for p in paragraphs]
    word_lengths = [n for n in word_lengths if n > 0]
    sent_counts = [len(split_sentences(p)) for p in paragraphs if p.strip()]
    words_mean, words_cv = _mean_cv(word_lengths)
    return {
        "paragraph.words_mean": words_mean,
        "paragraph.words_cv": words_cv,
        "paragraph.sentences_mean": statistics.mean(sent_counts) if sent_counts else 0.0,
    }


def _punctuation_features(text: str, word_count: int) -> dict[str, float]:
    sentence_count = max(len(split_sentences(text)), 1)
    return {
        "punctuation.commas_per_sentence": text.count(",") / sentence_count,
        "punctuation.em_dash_per_1000": _rate_per_1000(len(_EM_DASH_RE.findall(text)), word_count),
        "punctuation.semicolon_per_1000": _rate_per_1000(text.count(";"), word_count),
        "punctuation.ellipsis_per_1000": _rate_per_1000(len(_ELLIPSIS_RE.findall(text)), word_count),
        "punctuation.question_per_1000": _rate_per_1000(text.count("?"), word_count),
        "punctuation.exclamation_per_1000": _rate_per_1000(text.count("!"), word_count),
    }


def _function_word_features(tokens: list[str]) -> dict[str, float]:
    counts: Counter[str] = Counter(tokens)
    total = max(len(tokens), 1)
    return {
        f"fw.{word}": (counts.get(word, 0) / total) * 1000.0
        for word in load_function_words()
    }


def _windowed_ttr(tokens: list[str], window: int = TTR_WINDOW_TOKENS) -> float:
    if not tokens:
        return 0.0
    if len(tokens) <= window:
        return len(set(tokens)) / len(tokens)
    ratios = [
        len(set(tokens[i:i + window])) / window
        for i in range(0, len(tokens) - window + 1, window)
    ]
    return statistics.mean(ratios)


def _register_features(tokens: list[str]) -> dict[str, float]:
    total = max(len(tokens), 1)
    latinate = sum(
        1
        for t in tokens
        if len(t) >= _LATINATE_MIN_LEN and _LATINATE_SUFFIX_RE.search(t)
    )
    contractions = sum(1 for t in tokens if "'" in t or "’" in t)
    return {
        "register.word_len_mean": statistics.mean([len(t) for t in tokens]) if tokens else 0.0,
        "register.latinate_per_1000": (latinate / total) * 1000.0,
        "register.ttr_windowed": _windowed_ttr(tokens),
        "register.contraction_per_1000": (contractions / total) * 1000.0,
    }


def _dialogue_features(text: str, word_count: int) -> dict[str, float]:
    runs = [m.group(0) for m in _DIALOGUE_RE.finditer(text)]
    run_lengths = [len(TOKEN_RE.findall(r)) for r in runs]
    run_lengths = [n for n in run_lengths if n > 0]
    dialogue_words = sum(run_lengths)
    return {
        "dialogue.ratio": dialogue_words / max(word_count, 1),
        "dialogue.run_len_mean": statistics.mean(run_lengths) if run_lengths else 0.0,
    }


def extract_features(text: str) -> dict[str, float]:
    """Full stylometric feature vector for one stretch of masked prose.

    Deterministic: identical text always yields an identical mapping. The
    caller decides what "one stretch" is -- an exemplar segment, a whole
    chapter, or a paragraph window (see :data:`WINDOW_STABLE_BUCKETS` for
    which buckets are meaningful on windows).
    """
    tokens = [t.lower() for t in TOKEN_RE.findall(text)]
    word_count = len(tokens)
    vector: dict[str, float] = {}
    vector.update(_rhythm_features(text))
    vector.update(_paragraph_features(text))
    vector.update(_punctuation_features(text, word_count))
    vector.update(_function_word_features(tokens))
    vector.update(_register_features(tokens))
    vector.update(_dialogue_features(text, word_count))
    return vector
