"""Voice-drift scoring: chapter score, windowed findings, gate helper.

``run_voice(text, fingerprint)`` mirrors ``run_slop`` end to end: strip
frontmatter, mask code fences, measure, score 0-100 (0 = in voice), emit
:class:`~stoner.types.Finding` spans rebased onto the raw file, and return a
:class:`~stoner.types.VoiceReport`. Scoring is pure arithmetic: per feature,
a z-distance against the fingerprint's dispersion, mapped through a
``_rate_score``-shaped curve (0 inside one standard deviation, 50 at ~2,
~90 at ~3.5) and combined via a weighted mean per bucket. The function-word
profile is scored as one composite Burrows'-Delta-style mean absolute z.

Short documents and thin fingerprints damp the score toward 0 rather than
convicting on noise. Paragraph-window findings cover ~120-word stretches of
consecutive paragraphs, scored on window-stable buckets only, and name the
top drifting features with measured-vs-fingerprint values.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

from ..project import WritingProject, count_words, split_frontmatter
from ..slop import _rebase_finding
from ..slop.analyzers import TOKEN_RE, mask_code_fences
from ..types import Finding, Severity, Span, VoiceReport
from .features import (
    WINDOW_STABLE_BUCKETS,
    bucket_of,
    extract_features,
)
from .fingerprint import (
    FeatureStat,
    Fingerprint,
    FingerprintError,
    load_fingerprint,
    render_digest,
)
from .report import verdict

if TYPE_CHECKING:  # pragma: no cover - typing only, avoids a config import cycle
    from ..config import VoiceConfig

#: Weighted-mean weights for the overall score. Must sum to 1.0.
DEFAULT_WEIGHTS: dict[str, float] = {
    "rhythm": 0.20,
    "paragraph": 0.10,
    "punctuation": 0.15,
    "function_words": 0.25,
    "register": 0.20,
    "dialogue": 0.10,
}

#: Below this word count the score is damped linearly toward 0 -- a few
#: hundred words is not enough signal to convict a chapter of breaking voice
#: (same idea as slop's SHORT_DOC_WORD_THRESHOLD).
SHORT_DOC_WORD_THRESHOLD = 300

#: Multiplier applied when the fingerprint was learned from a thin corpus.
THIN_FINGERPRINT_DAMPING = 0.75

#: Dispersion floors: std is clamped to at least this fraction of |mean|,
#: plus a small per-family absolute floor, so a near-zero exemplar std (tiny
#: corpora, rare punctuation) cannot turn noise into a conviction.
STD_FLOOR_REL = 0.15
STD_FLOOR_ABS: dict[str, float] = {
    "rhythm": 0.75,
    "paragraph": 2.0,
    "punctuation": 0.6,
    "function_words": 1.0,
    "register": 0.05,
    "dialogue": 0.03,
}

#: Paragraph windows accumulate consecutive paragraphs to at least this many
#: words before being scored -- single paragraphs are statistically too short.
WINDOW_MIN_WORDS = 120

#: Window drift score thresholds for emitting a Finding.
WINDOW_MINOR_SCORE = 30.0
WINDOW_MAJOR_SCORE = 60.0

#: Cap on window findings per document.
MAX_WINDOW_FINDINGS = 12

#: How many drifting features a finding names.
_TOP_FEATURES_PER_FINDING = 3

_PARA_RE = re.compile(r"[^\n](?:[^\n]|\n(?!\s*\n))*")

#: Directional suggestions for the most interpretable features:
#: (measured above fingerprint, measured below fingerprint).
_SUGGESTIONS: dict[str, tuple[str, str]] = {
    "rhythm.sentence_len_mean": ("shorten sentences", "let sentences run longer"),
    "rhythm.sentence_len_cv": ("even out sentence-length swings", "vary sentence length more"),
    "rhythm.short_sentence_rate": ("cut some clipped one-beat sentences", "add short punchy sentences"),
    "rhythm.long_sentence_rate": ("break up the longest sentences", "let more sentences breathe"),
    "punctuation.commas_per_sentence": ("simplify comma-laden sentences", "restore clause rhythm with commas"),
    "punctuation.em_dash_per_1000": ("cut em-dashes", "this voice leans on em-dashes more"),
    "punctuation.semicolon_per_1000": ("cut semicolons", "this voice uses semicolons more"),
    "punctuation.ellipsis_per_1000": ("cut ellipses", "this voice trails off more"),
    "punctuation.question_per_1000": ("cut rhetorical questions", "this voice asks more questions"),
    "punctuation.exclamation_per_1000": ("cut exclamation marks", "this voice exclaims more"),
    "register.word_len_mean": ("prefer shorter, plainer words", "this voice runs to longer words"),
    "register.latinate_per_1000": ("swap latinate diction for plain words", "this voice is more latinate"),
    "register.ttr_windowed": ("rein in vocabulary sprawl", "vary word choice more"),
    "register.contraction_per_1000": ("use fewer contractions", "use contractions the way the exemplars do"),
}


# ---------------------------------------------------------------------------
# z-distance and curve
# ---------------------------------------------------------------------------


def _std_floor(feature: str, stat: FeatureStat) -> float:
    abs_floor = STD_FLOOR_ABS.get(bucket_of(feature), 0.05)
    return max(stat.std, STD_FLOOR_REL * abs(stat.mean), abs_floor)


def _z_for(feature: str, value: float, stat: FeatureStat) -> float:
    return abs(value - stat.mean) / _std_floor(feature, stat)


def _score_from_z(z: float) -> float:
    """Map a z-distance to 0-100: 0 inside one std, 50 at ~2, ~90 at ~3.5."""
    if z <= 1.0:
        return 0.0
    if z <= 2.0:
        return 50.0 * (z - 1.0)
    if z <= 3.5:
        return 50.0 + 40.0 * (z - 2.0) / 1.5
    return min(100.0, 90.0 + 10.0 * (z - 3.5) / 3.5)


def _bucket_scores(
    vector: dict[str, float], fingerprint: Fingerprint
) -> tuple[dict[str, float], list[tuple[str, float, FeatureStat, float]]]:
    """Per-bucket 0-100 subscores plus per-feature drift details.

    Non-function-word buckets average their features' curve scores; the
    function-word bucket runs its composite mean-absolute-z (Burrows'-Delta
    style) through the same curve. Returns ``(subscores, details)`` where
    details are ``(feature, value, stat, z)`` sorted by z descending
    (individual fw.* features excluded -- they only act through the
    composite).
    """
    per_bucket_scores: dict[str, list[float]] = {}
    fw_zs: list[float] = []
    details: list[tuple[str, float, FeatureStat, float]] = []
    for name, stat in fingerprint.features.items():
        if name not in vector:
            continue
        value = vector[name]
        z = _z_for(name, value, stat)
        bucket = bucket_of(name)
        if bucket == "function_words":
            fw_zs.append(z)
            continue
        per_bucket_scores.setdefault(bucket, []).append(_score_from_z(z))
        details.append((name, value, stat, z))
    subscores = {b: sum(s) / len(s) for b, s in per_bucket_scores.items() if s}
    if fw_zs:
        delta = sum(fw_zs) / len(fw_zs)
        subscores["function_words"] = _score_from_z(delta)
        details.append(
            ("function_words.delta", delta, FeatureStat(mean=0.0, std=1.0), delta)
        )
    details.sort(key=lambda d: d[3], reverse=True)
    return subscores, details


def _weighted_mean(subscores: dict[str, float], weights: dict[str, float]) -> float:
    total = sum(weights.get(b, 0.0) for b in subscores) or 1.0
    return sum(subscores[b] * weights.get(b, 0.0) for b in subscores) / total


def _fmt(value: float) -> str:
    return f"{value:.2f}".rstrip("0").rstrip(".")


def _describe(details: list[tuple[str, float, FeatureStat, float]], limit: int) -> str:
    parts = []
    for name, value, stat, _z in details[:limit]:
        if name == "function_words.delta":
            parts.append(f"function-word profile delta {_fmt(value)} (in-voice ~<1)")
        else:
            parts.append(f"{name} measured {_fmt(value)} vs {_fmt(stat.mean)}±{_fmt(stat.std)}")
    return "; ".join(parts)


def _suggest(details: list[tuple[str, float, FeatureStat, float]]) -> str:
    for name, value, stat, _z in details:
        directions = _SUGGESTIONS.get(name)
        if directions is None:
            continue
        return directions[0] if value > stat.mean else directions[1]
    return "revise this passage toward the fingerprint values named above"


# ---------------------------------------------------------------------------
# Windowed findings
# ---------------------------------------------------------------------------


def _paragraph_spans(body: str) -> list[tuple[int, int]]:
    return [m.span() for m in _PARA_RE.finditer(body) if m.group(0).strip()]


def _windows(body: str) -> list[tuple[int, int]]:
    """Greedy windows of consecutive paragraphs, >= WINDOW_MIN_WORDS each.

    A short trailing run is merged into the previous window so nothing is
    scored on a fragment.
    """
    spans = _paragraph_spans(body)
    windows: list[tuple[int, int]] = []
    start: int | None = None
    words = 0
    for s, e in spans:
        if start is None:
            start = s
        words += len(TOKEN_RE.findall(body[s:e]))
        if words >= WINDOW_MIN_WORDS:
            windows.append((start, e))
            start, words = None, 0
    if start is not None:
        if windows and words < WINDOW_MIN_WORDS:
            windows[-1] = (windows[-1][0], spans[-1][1])
        else:
            windows.append((start, spans[-1][1]))
    return windows


def _window_findings(
    body: str, fingerprint: Fingerprint, weights: dict[str, float]
) -> list[Finding]:
    window_weights = {b: weights.get(b, 0.0) for b in WINDOW_STABLE_BUCKETS}
    findings: list[Finding] = []
    scored: list[tuple[float, tuple[int, int], list[tuple[str, float, FeatureStat, float]]]] = []
    for start, end in _windows(body):
        vector = extract_features(body[start:end])
        subscores, details = _bucket_scores(vector, fingerprint)
        subscores = {b: s for b, s in subscores.items() if b in WINDOW_STABLE_BUCKETS}
        if not subscores:
            continue
        score = _weighted_mean(subscores, window_weights)
        if score >= WINDOW_MINOR_SCORE:
            scored.append((score, (start, end), details))
    scored.sort(key=lambda item: item[0], reverse=True)
    for score, (start, end), details in scored[:MAX_WINDOW_FINDINGS]:
        top = [d for d in details if bucket_of(d[0]) in WINDOW_STABLE_BUCKETS or d[0] == "function_words.delta"]
        source_bucket = bucket_of(top[0][0]) if top else "rhythm"
        if top and top[0][0] == "function_words.delta":
            source_bucket = "function_words"
        severity = Severity.major if score >= WINDOW_MAJOR_SCORE else Severity.minor
        findings.append(
            Finding(
                source=f"voice:{source_bucket}",
                severity=severity,
                category="drift_window",
                span=Span(start=start, end=end, line=1),  # line rebased by caller
                quote=body[start:end],
                issue=(
                    f"voice drift {score:.0f}/100 in this passage: "
                    + _describe(top, _TOP_FEATURES_PER_FINDING)
                ),
                suggestion=_suggest(top),
            )
        )
    return findings


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def run_voice(
    text: str,
    fingerprint: Fingerprint | None,
    path: str = "",
    config: VoiceConfig | None = None,
) -> VoiceReport:
    """Score one document's raw text against the learned fingerprint.

    ``text`` is the raw file content (markdown chapter, possibly with YAML
    frontmatter). Frontmatter is stripped and fenced code blocks masked
    before measurement; findings carry spans/lines mapped back onto the
    original ``text`` so a UI can highlight the source file directly.
    Raises :class:`FingerprintError` when ``fingerprint`` is None (R8: the
    direct check surface fails actionably; integrations degrade instead).
    """
    if fingerprint is None:
        raise FingerprintError(
            "No voice fingerprint to check against. Run `stoner voice learn` first."
        )
    weights = dict(config.weights) if config is not None and config.weights else dict(DEFAULT_WEIGHTS)
    _fm, body = split_frontmatter(text)
    body_start = len(text) - len(body)
    masked = mask_code_fences(body)
    word_count = count_words(masked)

    notes: list[str] = []
    if word_count == 0:
        report = VoiceReport(path=path, score=0.0, subscores={}, findings=[])
        report.stats = {"word_count": 0, "verdict": verdict(0.0), "notes": ["empty document"]}
        return report

    vector = extract_features(masked)
    subscores, details = _bucket_scores(vector, fingerprint)
    score = _weighted_mean(subscores, weights)

    damping = 1.0
    if word_count < SHORT_DOC_WORD_THRESHOLD:
        damping *= word_count / SHORT_DOC_WORD_THRESHOLD
        notes.append(
            f"short document ({word_count} words < {SHORT_DOC_WORD_THRESHOLD}): score damped"
        )
    if fingerprint.thin:
        damping *= THIN_FINGERPRINT_DAMPING
        notes.append(
            f"thin fingerprint ({fingerprint.total_words} words < 5000 exemplar words): score damped"
        )
    score = round(max(0.0, min(100.0, score * damping)), 2)

    findings = _window_findings(masked, fingerprint, weights)
    for finding in findings:
        _rebase_finding(finding, text, body_start)

    stats: dict[str, Any] = {
        "word_count": word_count,
        "verdict": verdict(score),
        "damping": round(damping, 3),
        "notes": notes,
        "fingerprint": {
            "segment_count": fingerprint.segment_count,
            "total_words": fingerprint.total_words,
            "thin": fingerprint.thin,
        },
        "top_features": [
            {
                "feature": name,
                "value": round(value, 4),
                "mean": round(stat.mean, 4),
                "std": round(stat.std, 4),
                "z": round(z, 3),
            }
            for name, value, stat, z in details[:10]
        ],
    }
    return VoiceReport(path=path, score=score, subscores=subscores, findings=findings, stats=stats)


def voice_context_digest(project: WritingProject, body: str) -> str:
    """Fingerprint digest plus this document's top measured drift, or "".

    The advisory surfaces (review passes, prompts) call this: a missing or
    broken fingerprint degrades silently to the empty string (R8), never an
    exception. Bounded in length for prompt budgets.
    """
    try:
        fingerprint = load_fingerprint(project)
    except FingerprintError:
        return ""
    text = render_digest(fingerprint)
    report = run_voice(body, fingerprint, config=project.config.voice)
    drift_lines = [
        f"- {t['feature']}: measured {t['value']} vs {t['mean']}±{t['std']} (z={t['z']})"
        for t in report.stats.get("top_features", [])[:5]
    ]
    if drift_lines:
        text += (
            f"\nMeasured drift for this chapter: {report.score:.1f}/100 "
            f"({report.stats.get('verdict', '')}). Top drifting features:\n"
            + "\n".join(drift_lines)
        )
    return text


def voice_gate_check(
    project: WritingProject, body: str, path: str = ""
) -> tuple[VoiceReport | None, bool]:
    """Deterministic gate helper for the write pipeline.

    Returns ``(report, fails)``. When ``voice.gate`` is off (the default) or
    no fingerprint exists, returns ``(None, False)`` -- the pipeline behaves
    exactly as it does today (R8 degradation). ``fails`` is True when the
    drift score exceeds ``voice.max_drift_score``.
    """
    cfg = project.config.voice
    if not cfg.gate:
        return None, False
    try:
        fingerprint = load_fingerprint(project)
    except FingerprintError:
        return None, False
    report = run_voice(body, fingerprint, path=path, config=cfg)
    return report, report.score > cfg.max_drift_score
