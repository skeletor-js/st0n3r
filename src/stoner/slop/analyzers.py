"""Deterministic prose analyzers for the slop detector.

Every analyzer takes the already-frontmatter-stripped, code-fence-masked
document body (see :mod:`stoner.slop`) in *local* coordinates and returns an
:class:`AnalyzerResult`: a list of :class:`~stoner.types.Finding` (with spans
still in local/body coordinates -- the caller in ``slop/__init__.py`` shifts
them to offsets into the original file and fills in line numbers) plus a
``stats`` dict of raw counts/rates that :mod:`stoner.slop.score` normalizes
into a 0-100 subscore.

Thresholds here govern *finding emission* (should we surface an occurrence
to the user at all, and at what severity). They are deliberately separate
from the continuous scoring curves in ``score.py``, which may use their own
constants tuned for smooth 0-100 output.
"""

from __future__ import annotations

import re
import statistics
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Any

from ..types import Finding, Severity
from .lexicon import PatternEntry, PhraseEntry, WordEntry

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

#: Matches a fenced code block (``` or ~~~, >=3 chars, indented up to 3
#: spaces) so its contents can be masked out of analysis.
FENCE_RE = re.compile(r"^([ \t]{0,3})(`{3,}|~{3,})[^\n]*\n.*?^\1\2[ \t]*$", re.MULTILINE | re.DOTALL)

#: Content-word tokenizer. Deliberately simple (ASCII letters + internal
#: apostrophe/hyphen); good enough for rate calculations and n-gram work.
TOKEN_RE = re.compile(r"[A-Za-z]+(?:['’-][A-Za-z]+)*")

#: Sentence-ending punctuation run, optionally followed by closing quotes.
_SENT_END_RE = re.compile(r"[.!?]+[\"'’”)\]]*")

#: Short titles/abbreviations that end in a period but do not end a sentence.
ABBREVIATIONS = {
    "mr", "mrs", "ms", "dr", "st", "jr", "sr", "prof", "vs", "etc", "mt",
    "ft", "capt", "gen", "col", "lt", "sgt", "rev", "fig", "approx", "dept",
    "est", "misc", "no", "vol", "ave", "blvd",
}

#: Minimal stopword list used to filter n-grams/echo detection.
STOPWORDS = {
    "a", "an", "the", "and", "or", "but", "nor", "so", "yet", "of", "in",
    "on", "at", "to", "for", "with", "as", "by", "from", "up", "down",
    "is", "was", "were", "are", "be", "been", "being", "am", "it", "its",
    "he", "she", "they", "them", "his", "her", "their", "him", "i", "we",
    "you", "your", "my", "me", "us", "our", "this", "that", "these",
    "those", "not", "no", "if", "then", "than", "too", "very", "just",
    "into", "out", "about", "over", "under", "again", "there", "here",
    "what", "who", "whom", "which", "when", "where", "why", "how", "all",
    "any", "both", "each", "few", "more", "most", "other", "some", "such",
    "only", "own", "same", "will", "would", "can", "could", "should",
    "shall", "may", "might", "must", "do", "does", "did", "have", "has",
    "had", "had", "one",
}


def mask_code_fences(body: str) -> str:
    """Replace fenced code block contents with spaces, preserving offsets.

    Newlines are kept (so line numbers stay correct); every other character
    inside a fence becomes a space so lexicon/pattern regexes can never match
    inside code, while everything outside a fence is untouched -- slices of
    the masked text outside fences are byte-identical to the source.
    """

    def _blank(m: re.Match[str]) -> str:
        return "".join(ch if ch == "\n" else " " for ch in m.group(0))

    return FENCE_RE.sub(_blank, body)


def iter_tokens(text: str) -> list[tuple[str, tuple[int, int]]]:
    """Return ``(lowercased_token, (start, end))`` for every word in text."""
    return [(m.group(0).lower(), m.span()) for m in TOKEN_RE.finditer(text)]


def split_sentences(text: str) -> list[tuple[int, int]]:
    """Split text into sentence ``(start, end)`` spans (local coordinates).

    A pragmatic, non-perfect splitter: breaks on `.`/`!`/`?` runs followed by
    whitespace, skipping breaks that immediately follow a known abbreviation
    (``Mr.``, ``Dr.``, ...). Leading/trailing whitespace on each span is
    trimmed. Good enough for burstiness/rhythm and echo analysis; not a
    linguistic sentence tokenizer.
    """
    spans: list[tuple[int, int]] = []
    pos = 0
    n = len(text)
    for m in _SENT_END_RE.finditer(text):
        end = m.end()
        # Only treat this as a boundary if followed by whitespace or EOF.
        if end < n and not text[end].isspace():
            continue
        pre = text[pos:m.start()]
        word_m = re.search(r"([A-Za-z]+)$", pre)
        if word_m and word_m.group(1).lower() in ABBREVIATIONS:
            continue
        s, e = pos, end
        while s < e and text[s].isspace():
            s += 1
        if s < e:
            spans.append((s, e))
        pos = end
    if pos < n:
        s, e = pos, n
        while s < e and text[s].isspace():
            s += 1
        while e > s and text[e - 1].isspace():
            e -= 1
        if s < e:
            spans.append((s, e))
    return spans


def _finding(
    source: str,
    severity: Severity,
    category: str,
    span: tuple[int, int] | None,
    text: str,
    *,
    issue: str,
    suggestion: str = "",
) -> Finding:
    """Build a Finding with a *local* span; caller rebases to global offsets."""
    from ..types import Span  # local import avoids polluting module namespace

    if span is None:
        return Finding(source=source, severity=severity, category=category, issue=issue, suggestion=suggestion)
    start, end = span
    return Finding(
        source=source,
        severity=severity,
        category=category,
        span=Span(start=start, end=end, line=1),  # line rebased by caller
        quote=text[start:end],
        issue=issue,
        suggestion=suggestion,
    )


@dataclass
class AnalyzerResult:
    findings: list[Finding] = field(default_factory=list)
    stats: dict[str, Any] = field(default_factory=dict)


def _rate_per_1000(count: int, word_count: int) -> float:
    return (count / max(word_count, 1)) * 1000.0


# ---------------------------------------------------------------------------
# 1-3: lexicon words / phrases / regex patterns
# ---------------------------------------------------------------------------

#: Cap on how many occurrences of a single term/phrase/pattern produce a
#: Finding -- prevents one overused word from flooding the report. All
#: occurrences still count toward stats/score.
DEDUPE_CAP_PER_TERM = 5

_SEVERITY_WEIGHT = {
    Severity.info: 0.5,
    Severity.minor: 1.0,
    Severity.major: 2.0,
    Severity.critical: 3.5,
}


def analyze_words(text: str, entries: list[WordEntry], *, cap: int = DEDUPE_CAP_PER_TERM) -> AnalyzerResult:
    findings: list[Finding] = []
    counts: Counter[str] = Counter()
    weighted = 0.0
    by_severity: Counter[str] = Counter()
    for entry in entries:
        matches = list(entry.regex.finditer(text))
        if not matches:
            continue
        counts[entry.term] = len(matches)
        weighted += _SEVERITY_WEIGHT[entry.severity] * len(matches)
        by_severity[entry.severity.value] += len(matches)
        note = f" -- {entry.note}" if entry.note else ""
        for m in matches[:cap]:
            findings.append(
                _finding(
                    "slop:lexicon", entry.severity, "word", m.span(), text,
                    issue=f"overused word: '{entry.term}'{note}",
                )
            )
    stats = {
        "occurrences": sum(counts.values()),
        "unique_terms": len(counts),
        "weighted_occurrences": weighted,
        "by_severity": dict(by_severity),
        "top_terms": counts.most_common(10),
    }
    return AnalyzerResult(findings=findings, stats=stats)


def analyze_phrases(text: str, entries: list[PhraseEntry], *, cap: int = DEDUPE_CAP_PER_TERM) -> AnalyzerResult:
    findings: list[Finding] = []
    counts: Counter[str] = Counter()
    weighted = 0.0
    by_severity: Counter[str] = Counter()
    for entry in entries:
        matches = list(entry.regex.finditer(text))
        if not matches:
            continue
        counts[entry.phrase] = len(matches)
        weighted += _SEVERITY_WEIGHT[entry.severity] * len(matches)
        by_severity[entry.severity.value] += len(matches)
        note = f" -- {entry.note}" if entry.note else ""
        for m in matches[:cap]:
            findings.append(
                _finding(
                    "slop:phrases", entry.severity, "phrase", m.span(), text,
                    issue=f"cliché phrase: '{entry.phrase}'{note}",
                )
            )
    stats = {
        "occurrences": sum(counts.values()),
        "unique_phrases": len(counts),
        "weighted_occurrences": weighted,
        "by_severity": dict(by_severity),
        "top_phrases": counts.most_common(10),
    }
    return AnalyzerResult(findings=findings, stats=stats)


def analyze_patterns(text: str, entries: list[PatternEntry], *, cap: int = DEDUPE_CAP_PER_TERM) -> AnalyzerResult:
    findings: list[Finding] = []
    counts: Counter[str] = Counter()
    weighted = 0.0
    by_severity: Counter[str] = Counter()
    for entry in entries:
        matches = list(entry.regex.finditer(text))
        if not matches:
            continue
        counts[entry.name] = len(matches)
        weighted += _SEVERITY_WEIGHT[entry.severity] * len(matches)
        by_severity[entry.severity.value] += len(matches)
        note = f" -- {entry.note}" if entry.note else ""
        for m in matches[:cap]:
            findings.append(
                _finding(
                    "slop:patterns", entry.severity, entry.name, m.span(), text,
                    issue=f"construction '{entry.name}'{note}",
                )
            )
    stats = {
        "occurrences": sum(counts.values()),
        "unique_patterns": len(counts),
        "weighted_occurrences": weighted,
        "by_severity": dict(by_severity),
        "top_patterns": counts.most_common(10),
    }
    return AnalyzerResult(findings=findings, stats=stats)


# ---------------------------------------------------------------------------
# 4: punctuation habits
# ---------------------------------------------------------------------------

EM_DASH_MINOR_PER_1000 = 6.0
EM_DASH_MAJOR_PER_1000 = 12.0
SEMICOLON_MINOR_PER_1000 = 4.0
SEMICOLON_MAJOR_PER_1000 = 8.0
ELLIPSIS_MINOR_PER_1000 = 3.0
ELLIPSIS_MAJOR_PER_1000 = 8.0
EXCLAIM_MINOR_PER_1000 = 3.0
EXCLAIM_MAJOR_PER_1000 = 8.0

_EM_DASH_RE = re.compile(r"—|--(?!-)")
_SEMICOLON_RE = re.compile(r";")
_ELLIPSIS_RE = re.compile(r"\.\.\.|…")
_EXCLAIM_RE = re.compile(r"!")


def analyze_punctuation(text: str, word_count: int) -> AnalyzerResult:
    findings: list[Finding] = []
    em_dashes = [m.span() for m in _EM_DASH_RE.finditer(text)]
    semicolons = [m.span() for m in _SEMICOLON_RE.finditer(text)]
    ellipses = [m.span() for m in _ELLIPSIS_RE.finditer(text)]
    exclaims = [m.span() for m in _EXCLAIM_RE.finditer(text)]

    def _flag(name: str, category: str, spans: list[tuple[int, int]], minor_th: float, major_th: float) -> float:
        rate = _rate_per_1000(len(spans), word_count)
        if rate > major_th:
            sev = Severity.major
        elif rate > minor_th:
            sev = Severity.minor
        else:
            return rate
        span = spans[0] if spans else None
        findings.append(
            _finding(
                "slop:punctuation", sev, category, span, text,
                issue=f"{name} rate {rate:.1f}/1000 words ({len(spans)} total) is elevated",
            )
        )
        return rate

    em_rate = _flag("em-dash", "em_dash", em_dashes, EM_DASH_MINOR_PER_1000, EM_DASH_MAJOR_PER_1000)
    semi_rate = _flag("semicolon", "semicolon", semicolons, SEMICOLON_MINOR_PER_1000, SEMICOLON_MAJOR_PER_1000)
    ell_rate = _flag("ellipsis", "ellipsis", ellipses, ELLIPSIS_MINOR_PER_1000, ELLIPSIS_MAJOR_PER_1000)
    exc_rate = _flag("exclamation", "exclamation", exclaims, EXCLAIM_MINOR_PER_1000, EXCLAIM_MAJOR_PER_1000)

    stats = {
        "em_dash_count": len(em_dashes), "em_dash_per_1000": em_rate,
        "semicolon_count": len(semicolons), "semicolon_per_1000": semi_rate,
        "ellipsis_count": len(ellipses), "ellipsis_per_1000": ell_rate,
        "exclamation_count": len(exclaims), "exclamation_per_1000": exc_rate,
    }
    return AnalyzerResult(findings=findings, stats=stats)


# ---------------------------------------------------------------------------
# 5: repetition
# ---------------------------------------------------------------------------

NGRAM_SIZES = (3, 4)
NGRAM_MIN_OCCURRENCES = 3
NGRAM_FINDING_CAP = 3
ECHO_WINDOW_TOKENS = 40
ECHO_MIN_COUNT = 3
ECHO_FINDING_CAP = 15
SENTENCE_START_ECHO_MIN = 3


def _severity_for_repeat_count(count: int) -> Severity:
    if count >= 7:
        return Severity.critical
    if count >= 5:
        return Severity.major
    return Severity.minor


def analyze_repetition(text: str) -> AnalyzerResult:
    toks = iter_tokens(text)
    words = [w for w, _ in toks]
    findings: list[Finding] = []

    # -- repeated content n-grams -------------------------------------
    ngram_hits: dict[str, int] = {}
    for n in NGRAM_SIZES:
        seen: dict[str, list[int]] = defaultdict(list)
        for i in range(len(words) - n + 1):
            gram = words[i:i + n]
            if all(w in STOPWORDS for w in gram):
                continue
            seen[" ".join(gram)].append(i)
        for key, idxs in seen.items():
            if len(idxs) < NGRAM_MIN_OCCURRENCES:
                continue
            ngram_hits[key] = len(idxs)
            sev = _severity_for_repeat_count(len(idxs))
            for i in idxs[:NGRAM_FINDING_CAP]:
                start = toks[i][1][0]
                end = toks[i + n - 1][1][1]
                findings.append(
                    _finding(
                        "slop:repetition", sev, "ngram", (start, end), text,
                        issue=f"repeated {n}-gram '{key}' occurs {len(idxs)}x in the document",
                    )
                )

    # -- same-word echo within a sliding window ------------------------
    echo_count = 0
    last_flagged: dict[str, int] = {}
    for i, (w, span) in enumerate(toks):
        if w in STOPWORDS or len(w) < 4:
            continue
        window = words[i:i + ECHO_WINDOW_TOKENS]
        occ = window.count(w)
        if occ < ECHO_MIN_COUNT:
            continue
        if i - last_flagged.get(w, -10_000) < ECHO_WINDOW_TOKENS:
            continue
        last_flagged[w] = i
        echo_count += 1
        if echo_count <= ECHO_FINDING_CAP:
            end_idx = min(i + ECHO_WINDOW_TOKENS, len(toks)) - 1
            findings.append(
                _finding(
                    "slop:repetition", Severity.minor, "word_echo",
                    (span[0], toks[end_idx][1][1]), text,
                    issue=f"word '{w}' repeated {occ}x within a {ECHO_WINDOW_TOKENS}-word window",
                )
            )

    # -- consecutive sentence-start echo --------------------------------
    sentences = split_sentences(text)
    start_words: list[str | None] = []
    for s, e in sentences:
        m = re.match(r"[A-Za-z']+", text[s:e])
        start_words.append(m.group(0).lower() if m else None)

    start_echo_runs = 0
    run_start = 0
    i = 1
    n_sent = len(sentences)
    while i <= n_sent:
        same = i < n_sent and start_words[i] is not None and start_words[i] == start_words[run_start]
        if same:
            i += 1
            continue
        run_len = i - run_start
        if run_len >= SENTENCE_START_ECHO_MIN and start_words[run_start]:
            start_echo_runs += 1
            span = (sentences[run_start][0], sentences[i - 1][1])
            findings.append(
                _finding(
                    "slop:repetition", Severity.minor, "sentence_start_echo", span, text,
                    issue=f"{run_len} consecutive sentences start with '{start_words[run_start]}'",
                )
            )
        run_start = i
        i += 1

    stats = {
        "repeated_ngrams": len(ngram_hits),
        "repeated_ngram_total_occurrences": sum(ngram_hits.values()),
        "word_echo_count": echo_count,
        "sentence_start_echo_runs": start_echo_runs,
    }
    return AnalyzerResult(findings=findings, stats=stats)


# ---------------------------------------------------------------------------
# 6: rhythm / burstiness
# ---------------------------------------------------------------------------

MIN_SENTENCES_FOR_CV = 20
CV_UNIFORM_THRESHOLD = 0.35
RUN_LENGTH_MIN = 4
RUN_TOLERANCE_WORDS = 2
MIN_PARAGRAPHS_FOR_CV = 6
PARAGRAPH_CV_THRESHOLD = 0.35

_PARA_SPLIT_RE = re.compile(r"\n\s*\n")


def _sentence_word_count(text: str, span: tuple[int, int]) -> int:
    return len(TOKEN_RE.findall(text[span[0]:span[1]]))


def analyze_rhythm(text: str) -> AnalyzerResult:
    findings: list[Finding] = []
    sentences = split_sentences(text)
    lengths = [_sentence_word_count(text, s) for s in sentences]
    stats: dict[str, Any] = {"sentence_count": len(sentences)}

    cv: float | None = None
    if len(lengths) >= MIN_SENTENCES_FOR_CV:
        mean = statistics.mean(lengths)
        sd = statistics.pstdev(lengths)
        cv = (sd / mean) if mean else 0.0
        stats["sentence_length_cv"] = cv
        if cv < CV_UNIFORM_THRESHOLD:
            sev = Severity.major if cv < CV_UNIFORM_THRESHOLD * 0.6 else Severity.minor
            findings.append(
                _finding(
                    "slop:rhythm", sev, "uniform_sentence_length", None, text,
                    issue=f"sentence lengths are unusually uniform (CV={cv:.2f} over {len(lengths)} sentences)",
                )
            )

    # runs of 4+ consecutive near-equal-length sentences
    uniform_runs = 0
    run_start = 0
    i = 1
    while i <= len(lengths):
        close = i < len(lengths) and abs(lengths[i] - lengths[i - 1]) <= RUN_TOLERANCE_WORDS
        if close:
            i += 1
            continue
        run_len = i - run_start
        if run_len >= RUN_LENGTH_MIN:
            uniform_runs += 1
            span = (sentences[run_start][0], sentences[i - 1][1])
            findings.append(
                _finding(
                    "slop:rhythm", Severity.minor, "uniform_run", span, text,
                    issue=f"{run_len} consecutive sentences of near-equal length",
                )
            )
        run_start = i
        i += 1
    stats["uniform_runs"] = uniform_runs

    # paragraph length uniformity
    paragraphs = [p for p in _PARA_SPLIT_RE.split(text) if p.strip()]
    para_lengths = [len(TOKEN_RE.findall(p)) for p in paragraphs]
    if len(para_lengths) >= MIN_PARAGRAPHS_FOR_CV:
        pmean = statistics.mean(para_lengths)
        psd = statistics.pstdev(para_lengths)
        pcv = (psd / pmean) if pmean else 0.0
        stats["paragraph_length_cv"] = pcv
        if pcv < PARAGRAPH_CV_THRESHOLD:
            findings.append(
                _finding(
                    "slop:rhythm", Severity.minor, "uniform_paragraph_length", None, text,
                    issue=f"paragraph lengths are unusually uniform (CV={pcv:.2f} over {len(paragraphs)} paragraphs)",
                )
            )

    return AnalyzerResult(findings=findings, stats=stats)


# ---------------------------------------------------------------------------
# 7: density (adverbs, filter words, rule-of-three, adjective stacking)
# ---------------------------------------------------------------------------

ADVERB_RATE_FLAG_PER_1000 = 25.0
#: Common -ly words that are not the "creeping adverb" tell.
ADVERB_EXCLUDE = {
    "only", "family", "early", "likely", "friendly", "ugly", "holy", "silly",
    "belly", "rally", "ally", "reply", "supply", "apply", "imply", "lonely",
    "lovely", "lively", "homely", "unlikely", "monopoly", "assembly", "july",
    "italy", "jelly", "bully", "fully", "really", "namely", "hardly", "purely",
    "solely", "barely", "surely", "simply",
}
_ADVERB_RE = re.compile(r"\b([A-Za-z]+ly)\b")

FILTER_WORDS = {"felt", "saw", "seemed", "noticed", "realized", "watched", "wondered"}
FILTER_RATE_MINOR_PER_1000 = 6.0
FILTER_RATE_MAJOR_PER_1000 = 12.0
FILTER_FINDING_CAP = 5

RULE_OF_THREE_RE = re.compile(
    r"\b[A-Za-z]+(?:\s[A-Za-z]+){0,2},\s*[A-Za-z]+(?:\s[A-Za-z]+){0,2},?\s+and\s+[A-Za-z]+(?:\s[A-Za-z]+){0,2}\b"
)
RULE_OF_THREE_DENSITY_PER_300 = 2.0
RULE_OF_THREE_FINDING_CAP = 5

#: Heuristic: 3+ consecutive adjective-shaped words (common adjective
#: suffixes) followed by a noun-shaped word. Not real POS tagging.
_ADJ_SUFFIXES = r"(?:ous|ful|ive|al|ic|less|ish|able|ible|ary|ent|ant)"
ADJ_STACK_RE = re.compile(
    rf"\b(?:[A-Za-z]+{_ADJ_SUFFIXES},?\s+){{2,}}[A-Za-z]+{_ADJ_SUFFIXES}\s+[A-Za-z]+\b", re.IGNORECASE
)
ADJ_STACK_FINDING_CAP = 5


def analyze_density(text: str, word_count: int) -> AnalyzerResult:
    findings: list[Finding] = []

    # -- adverbs --------------------------------------------------------
    adverb_matches = [
        m for m in _ADVERB_RE.finditer(text) if m.group(1).lower() not in ADVERB_EXCLUDE
    ]
    adverb_rate = _rate_per_1000(len(adverb_matches), word_count)
    if adverb_rate > ADVERB_RATE_FLAG_PER_1000 and adverb_matches:
        sev = Severity.major if adverb_rate > ADVERB_RATE_FLAG_PER_1000 * 1.6 else Severity.minor
        findings.append(
            _finding(
                "slop:density", sev, "adverb_rate", adverb_matches[0].span(), text,
                issue=f"-ly adverb rate {adverb_rate:.1f}/1000 words ({len(adverb_matches)} total) is elevated",
            )
        )

    # -- filter words -----------------------------------------------------
    filter_matches = [
        m for m in re.finditer(r"\b([A-Za-z]+)\b", text) if m.group(1).lower() in FILTER_WORDS
    ]
    filter_rate = _rate_per_1000(len(filter_matches), word_count)
    by_word: Counter[str] = Counter(m.group(1).lower() for m in filter_matches)
    if filter_rate > FILTER_RATE_MINOR_PER_1000:
        sev = Severity.major if filter_rate > FILTER_RATE_MAJOR_PER_1000 else Severity.minor
        for m in filter_matches[:FILTER_FINDING_CAP]:
            findings.append(
                _finding(
                    "slop:density", sev, "filter_word", m.span(), text,
                    issue=f"filter word '{m.group(1)}' distances the reader from the scene",
                )
            )

    # -- rule of three ----------------------------------------------------
    three_matches = list(RULE_OF_THREE_RE.finditer(text))
    three_rate = (len(three_matches) / max(word_count, 1)) * 300.0
    if three_rate > RULE_OF_THREE_DENSITY_PER_300:
        for m in three_matches[:RULE_OF_THREE_FINDING_CAP]:
            findings.append(
                _finding(
                    "slop:density", Severity.minor, "rule_of_three", m.span(), text,
                    issue="'X, Y, and Z' triplet listing -- rule-of-three overuse",
                )
            )

    # -- adjective stacking -------------------------------------------------
    adj_matches = list(ADJ_STACK_RE.finditer(text))
    for m in adj_matches[:ADJ_STACK_FINDING_CAP]:
        findings.append(
            _finding(
                "slop:density", Severity.minor, "adjective_stacking", m.span(), text,
                issue="stacked adjectives before a noun",
            )
        )

    stats = {
        "adverb_count": len(adverb_matches),
        "adverb_per_1000": adverb_rate,
        "filter_word_count": len(filter_matches),
        "filter_word_per_1000": filter_rate,
        "filter_word_breakdown": dict(by_word),
        "rule_of_three_count": len(three_matches),
        "rule_of_three_per_300": three_rate,
        "adjective_stack_count": len(adj_matches),
    }
    return AnalyzerResult(findings=findings, stats=stats)
