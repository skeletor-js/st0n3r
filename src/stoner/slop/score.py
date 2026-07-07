"""Turn analyzer stats into a 0-100 slop score.

Each analyzer in :mod:`stoner.slop.analyzers` hands back raw counts/rates.
This module normalizes those rates against calibrated thresholds into a
0-100 subscore per category, combines them with a weighted mean, folds in a
small bonus for severe findings, and damps the result for very short
documents (where a couple of unlucky word choices would otherwise swing the
score wildly).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..types import Finding, Severity

#: Weighted-mean weights for the overall score. Must sum to 1.0.
DEFAULT_WEIGHTS: dict[str, float] = {
    "lexicon": 0.15,
    "phrases": 0.20,
    "patterns": 0.20,
    "punctuation": 0.10,
    "repetition": 0.15,
    "rhythm": 0.10,
    "density": 0.10,
}

#: Below this word count, dampen the final score toward 0 -- a handful of
#: tells in a 100-word snippet is not enough signal to convict a chapter.
SHORT_DOC_WORD_THRESHOLD = 200

#: Severity-count bonus added on top of the weighted mean (then clamped).
CRITICAL_BONUS = 3.0
MAJOR_BONUS = 1.0
MAX_SEVERITY_BONUS = 20.0

# -- per-category rate thresholds used by the normalization curve ----------
# (rate, in "weighted occurrences per 1000 words" unless noted) -> subscore
# hits 50 at *_MINOR and ~90 at *_MAJOR, asymptoting to 100 beyond that.

LEXICON_MINOR = 6.0
LEXICON_MAJOR = 20.0

PHRASES_MINOR = 3.0
PHRASES_MAJOR = 10.0

PATTERNS_MINOR = 3.0
PATTERNS_MAJOR = 10.0

REPETITION_MINOR = 2.0
REPETITION_MAJOR = 6.0

DENSITY_MINOR = 1.5
DENSITY_MAJOR = 4.0


@dataclass
class SlopConfig:
    """Tunable knobs for scoring. Passed through ``run_slop(config=...)``."""

    weights: dict[str, float] = field(default_factory=lambda: dict(DEFAULT_WEIGHTS))
    short_doc_word_threshold: int = SHORT_DOC_WORD_THRESHOLD


def _rate_score(rate: float, minor_th: float, major_th: float) -> float:
    """Map a rate to 0-100: 0 at rate=0, 50 at minor_th, ~90 at major_th."""
    if rate <= 0:
        return 0.0
    if minor_th <= 0:
        minor_th = 1e-6
    if rate <= minor_th:
        return 50.0 * (rate / minor_th)
    if major_th <= minor_th:
        major_th = minor_th * 2
    if rate <= major_th:
        return 50.0 + 40.0 * (rate - minor_th) / (major_th - minor_th)
    excess_ratio = (rate - major_th) / major_th
    return min(100.0, 90.0 + 10.0 * excess_ratio)


def score_lexicon(stats: dict[str, Any], word_count: int) -> float:
    rate = (stats.get("weighted_occurrences", 0.0) / max(word_count, 1)) * 1000.0
    return _rate_score(rate, LEXICON_MINOR, LEXICON_MAJOR)


def score_phrases(stats: dict[str, Any], word_count: int) -> float:
    rate = (stats.get("weighted_occurrences", 0.0) / max(word_count, 1)) * 1000.0
    return _rate_score(rate, PHRASES_MINOR, PHRASES_MAJOR)


def score_patterns(stats: dict[str, Any], word_count: int) -> float:
    rate = (stats.get("weighted_occurrences", 0.0) / max(word_count, 1)) * 1000.0
    return _rate_score(rate, PATTERNS_MINOR, PATTERNS_MAJOR)


def score_punctuation(stats: dict[str, Any], word_count: int) -> float:
    rates_thresholds = [
        (stats.get("em_dash_per_1000", 0.0), 6.0, 12.0),
        (stats.get("semicolon_per_1000", 0.0), 4.0, 8.0),
        (stats.get("ellipsis_per_1000", 0.0), 3.0, 8.0),
        (stats.get("exclamation_per_1000", 0.0), 3.0, 8.0),
    ]
    scores = [_rate_score(r, mn, mx) for r, mn, mx in rates_thresholds]
    return sum(scores) / len(scores) if scores else 0.0


def score_repetition(stats: dict[str, Any], word_count: int) -> float:
    weighted = (
        stats.get("repeated_ngram_total_occurrences", 0) * 1.0
        + stats.get("word_echo_count", 0) * 1.5
        + stats.get("sentence_start_echo_runs", 0) * 2.0
    )
    rate = (weighted / max(word_count, 1)) * 1000.0
    return _rate_score(rate, REPETITION_MINOR, REPETITION_MAJOR)


def score_rhythm(stats: dict[str, Any]) -> float:
    scores: list[float] = []
    cv = stats.get("sentence_length_cv")
    if cv is not None:
        threshold = 0.35
        deficit = max(0.0, threshold - cv) / threshold
        scores.append(min(100.0, deficit * 100.0))
    pcv = stats.get("paragraph_length_cv")
    if pcv is not None:
        threshold = 0.35
        deficit = max(0.0, threshold - pcv) / threshold
        scores.append(min(100.0, deficit * 100.0))
    runs = stats.get("uniform_runs", 0)
    if runs:
        scores.append(min(100.0, runs * 25.0))
    return sum(scores) / len(scores) if scores else 0.0


def score_density(stats: dict[str, Any], word_count: int) -> float:
    weighted = (
        stats.get("adverb_count", 0) * 0.4
        + stats.get("filter_word_count", 0) * 0.4
        + stats.get("rule_of_three_count", 0) * 1.5
        + stats.get("adjective_stack_count", 0) * 1.5
    )
    rate = (weighted / max(word_count, 1)) * 1000.0
    return _rate_score(rate, DENSITY_MINOR, DENSITY_MAJOR)


def combine(
    subscores: dict[str, float],
    findings: list[Finding],
    word_count: int,
    config: SlopConfig | None = None,
) -> float:
    """Weighted mean of subscores, plus severity bonus, plus short-doc damping."""
    config = config or SlopConfig()
    weights = config.weights
    total_weight = sum(weights.get(k, 0.0) for k in subscores) or 1.0
    weighted_mean = sum(subscores.get(k, 0.0) * weights.get(k, 0.0) for k in subscores) / total_weight

    critical_n = sum(1 for f in findings if f.severity == Severity.critical)
    major_n = sum(1 for f in findings if f.severity == Severity.major)
    bonus = min(MAX_SEVERITY_BONUS, critical_n * CRITICAL_BONUS + major_n * MAJOR_BONUS)

    score = min(100.0, weighted_mean + bonus)

    threshold = max(config.short_doc_word_threshold, 1)
    if word_count < threshold:
        damping = max(0.0, word_count / threshold)
        score *= damping

    return round(max(0.0, min(100.0, score)), 2)
