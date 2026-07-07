"""AI-slop detector: deterministic, no-LLM analysis of prose for LLM tells.

Public API:

- :func:`run_slop` -- analyze a markdown chapter's text, return a
  :class:`~stoner.types.SlopReport`.
- :func:`load_lexicon` -- load (and cache) the word/phrase/pattern lexicons.
"""

from __future__ import annotations

from ..project import count_words, split_frontmatter
from ..types import Finding, SlopReport
from .analyzers import (
    analyze_density,
    analyze_patterns,
    analyze_phrases,
    analyze_punctuation,
    analyze_repetition,
    analyze_rhythm,
    analyze_words,
    mask_code_fences,
)
from .lexicon import Lexicon, load_lexicon
from .report import verdict
from .score import (
    SlopConfig,
    combine,
    score_density,
    score_lexicon,
    score_patterns,
    score_phrases,
    score_punctuation,
    score_repetition,
    score_rhythm,
)

__all__ = ["run_slop", "load_lexicon", "SlopConfig", "Lexicon"]


def _rebase_finding(finding: Finding, original: str, body_start: int) -> Finding:
    """Shift a Finding's span from local body coordinates to the original text."""
    if finding.span is None:
        return finding
    start = body_start + finding.span.start
    end = body_start + finding.span.end
    line = original.count("\n", 0, start) + 1
    finding.span = finding.span.model_copy(update={"start": start, "end": end, "line": line})
    finding.quote = original[start:end]
    return finding


def run_slop(text: str, path: str = "", config: SlopConfig | None = None) -> SlopReport:
    """Run the full slop-detector pipeline over one document's raw text.

    ``text`` is the raw file content (markdown chapter, possibly with YAML
    frontmatter). Frontmatter is stripped and fenced code blocks are masked
    out before analysis; findings carry spans/lines mapped back onto the
    original, unstripped ``text`` so a UI can highlight the source file
    directly.
    """
    config = config or SlopConfig()
    _fm, body = split_frontmatter(text)
    body_start = len(text) - len(body)
    masked = mask_code_fences(body)
    word_count = count_words(masked)

    lex = load_lexicon()

    word_result = analyze_words(masked, list(lex.words))
    phrase_result = analyze_phrases(masked, list(lex.phrases))
    pattern_result = analyze_patterns(masked, list(lex.patterns))
    punct_result = analyze_punctuation(masked, word_count)
    repetition_result = analyze_repetition(masked)
    rhythm_result = analyze_rhythm(masked)
    density_result = analyze_density(masked, word_count)

    subscores = {
        "lexicon": score_lexicon(word_result.stats, word_count),
        "phrases": score_phrases(phrase_result.stats, word_count),
        "patterns": score_patterns(pattern_result.stats, word_count),
        "punctuation": score_punctuation(punct_result.stats, word_count),
        "repetition": score_repetition(repetition_result.stats, word_count),
        "rhythm": score_rhythm(rhythm_result.stats),
        "density": score_density(density_result.stats, word_count),
    }

    all_findings: list[Finding] = [
        *word_result.findings,
        *phrase_result.findings,
        *pattern_result.findings,
        *punct_result.findings,
        *repetition_result.findings,
        *rhythm_result.findings,
        *density_result.findings,
    ]

    overall = combine(subscores, all_findings, word_count, config)

    for finding in all_findings:
        _rebase_finding(finding, text, body_start)

    stats = {
        "word_count": word_count,
        "verdict": verdict(overall),
        "lexicon": word_result.stats,
        "phrases": phrase_result.stats,
        "patterns": pattern_result.stats,
        "punctuation": punct_result.stats,
        "repetition": repetition_result.stats,
        "rhythm": rhythm_result.stats,
        "density": density_result.stats,
    }

    return SlopReport(path=path, score=overall, subscores=subscores, findings=all_findings, stats=stats)
