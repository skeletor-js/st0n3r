"""Tests for the deterministic AI-slop detector (src/stoner/slop)."""

from __future__ import annotations

import re

import pytest

from stoner.slop import SlopConfig, load_lexicon, run_slop
from stoner.slop.analyzers import mask_code_fences, split_sentences
from stoner.slop.lexicon import load_patterns, load_phrases, load_words
from stoner.slop.report import render, verdict
from stoner.types import Severity

# ---------------------------------------------------------------------------
# Fixtures: one deliberately sloppy passage, one restrained literary one.
# ---------------------------------------------------------------------------

SLOPPY_TEXT = """---
title: The Sloppy Chapter
status: draft
---

Sarah couldn't help but delve into the tapestry of memories that haunted her.
It was a testament to her unwavering resolve. Little did she know, the
maelstrom of emotions would only grow stronger with each passing heartbeat.

Her eyes widened. Her eyes found his across the crowded room. A shiver ran
down her spine. She let out a breath she didn't know she'd been holding.
Something deep inside her stirred, restless and unrelenting.

The air was thick with tension. It was not just about survival, but about
redemption. She steeled herself, heart pounding in her chest, and stepped
into the unknown, into the labyrinthine corridors of her own fear.

She felt a wave of relief wash over her. She felt tired. She felt afraid.
She saw the door. She saw the light. She noticed the silence. She realized
she was alone. She watched the shadows. She wondered if it mattered at all.

Slowly, carefully, quietly, she moved forward, gently, deliberately, and
silently, into the dark. It was a mixture of fear and hope, of courage and
doubt, of despair and resolve. The tall, dark, mysterious figure loomed
ahead. The old, creaky, wooden door groaned as it opened, opened, opened.
"""

CLEAN_TEXT = """---
title: A Restrained Chapter
status: draft
---

William Stoner entered the University of Missouri as a freshman in the year
1910, at the age of nineteen. Eight years later, during the height of World
War I, he received his degree and accepted an instructorship at the same
University, where he taught until his death.

He did not rise above the rank of assistant professor, and few students
remembered him with any sharpness after they had taken his courses. When he
died his colleagues did not say much about him, and did not attend the
funeral in any great number.

His office was a small room on the second floor of the library. He had
worked there for thirty years, and in that time he had accumulated books and
papers until the shelves sagged under their weight. Students who came to see
him found him behind his desk, and he would look up slowly, as if returning
from a great distance.

He married in 1920. It was not a happy marriage. His wife came from a
family in St. Louis, and she had been raised to expect a different kind of
life. They lived together for many years without speaking of what had gone
wrong between them. He worked, and she kept the house, and their daughter
grew up in the space between them.

In his last years he thought often of the farm where he was born, of the
black earth and the long rows of corn, of his father and mother who had
worked that earth until it broke them. He remembered the cold mornings and
the smell of the barn. He remembered very little else with such clarity.
"""


# ---------------------------------------------------------------------------
# Lexicon integrity
# ---------------------------------------------------------------------------


def test_lexicon_loads_and_is_rich() -> None:
    lex = load_lexicon(force_reload=True)
    assert len(lex.words) >= 120
    assert len(lex.phrases) >= 150
    assert len(lex.patterns) >= 40


def test_every_word_entry_has_valid_severity_and_regex() -> None:
    for entry in load_words():
        assert isinstance(entry.severity, Severity)
        assert entry.regex.search(entry.term.lower())


def test_every_phrase_entry_has_valid_severity_and_regex() -> None:
    for entry in load_phrases():
        assert isinstance(entry.severity, Severity)
        assert entry.regex.search(entry.phrase.lower())


def test_every_pattern_compiles() -> None:
    patterns = load_patterns()
    for entry in patterns:
        # load_patterns() already compiles eagerly (raises on failure); this
        # also double-checks independently against the raw pattern string.
        re.compile(entry.pattern)
        assert isinstance(entry.severity, Severity)


def test_lexicon_cache_reload_returns_fresh_objects() -> None:
    a = load_lexicon()
    b = load_lexicon(force_reload=True)
    assert a is not b
    assert len(a.words) == len(b.words)


# ---------------------------------------------------------------------------
# End-to-end scoring on hand-written passages
# ---------------------------------------------------------------------------


def test_sloppy_passage_scores_high() -> None:
    report = run_slop(SLOPPY_TEXT, path="sloppy.md")
    assert report.score >= 55.0, report.score
    assert verdict(report.score) == "slop"


def test_sloppy_passage_flags_specific_tells() -> None:
    report = run_slop(SLOPPY_TEXT, path="sloppy.md")
    issues = " | ".join(f.issue.lower() for f in report.findings)
    categories = {f.category for f in report.findings}
    sources = {f.source for f in report.findings}

    assert "delve" in issues
    assert "tapestry" in issues
    assert "testament" in issues
    assert "couldn't help but" in issues
    assert "little did she know" in issues or "little_did_know" in categories
    assert "slop:lexicon" in sources
    assert "slop:phrases" in sources
    assert "slop:patterns" in sources
    assert "slop:density" in sources  # filter-word pile-up ("she felt/saw/noticed...")


def test_clean_passage_scores_low() -> None:
    report = run_slop(CLEAN_TEXT, path="clean.md")
    assert report.score <= 15.0, report.score
    assert verdict(report.score) in {"clean", "touched up"}


def test_sloppy_scores_higher_than_clean() -> None:
    sloppy = run_slop(SLOPPY_TEXT, path="sloppy.md")
    clean = run_slop(CLEAN_TEXT, path="clean.md")
    assert sloppy.score > clean.score


# ---------------------------------------------------------------------------
# Span correctness
# ---------------------------------------------------------------------------


def test_finding_spans_match_quoted_text() -> None:
    report = run_slop(SLOPPY_TEXT, path="sloppy.md")
    assert report.findings, "expected findings on the sloppy passage"
    for f in report.findings:
        if f.span is None:
            continue
        assert SLOPPY_TEXT[f.span.start:f.span.end] == f.quote
        assert f.span.start < f.span.end
        assert f.span.line >= 1


def test_finding_line_numbers_are_plausible() -> None:
    report = run_slop(SLOPPY_TEXT, path="sloppy.md")
    for f in report.findings:
        if f.span is None:
            continue
        # line N means there are exactly N-1 newlines before span.start
        assert SLOPPY_TEXT.count("\n", 0, f.span.start) + 1 == f.span.line


# ---------------------------------------------------------------------------
# Frontmatter / code-fence exclusion
# ---------------------------------------------------------------------------


def test_frontmatter_is_excluded_from_analysis() -> None:
    text = "---\ntitle: delve tapestry testament\nstatus: draft\n---\n\nA plain, quiet morning passed without incident.\n"
    report = run_slop(text, path="fm.md")
    assert report.findings == []
    assert report.stats["word_count"] < 15


def test_code_fences_are_excluded_from_analysis() -> None:
    body = (
        "Ordinary prose here, nothing unusual at all.\n\n"
        "```python\n"
        "# delve into the tapestry testament palpable myriad\n"
        "def f(): pass\n"
        "```\n\n"
        "More ordinary prose follows after the code.\n"
    )
    report = run_slop(body, path="code.md")
    assert report.findings == []


def test_mask_code_fences_preserves_length_and_newlines() -> None:
    body = "before\n```\ndelve tapestry\nmore code\n```\nafter\n"
    masked = mask_code_fences(body)
    assert len(masked) == len(body)
    assert masked.count("\n") == body.count("\n")
    assert "delve" not in masked
    assert masked.startswith("before\n")
    assert masked.rstrip("\n").endswith("after") or "after" in masked


def test_code_fence_content_does_not_shift_offsets_of_surrounding_text() -> None:
    body = "Sarah delved before.\n```\ndelve tapestry testament\n```\nSarah delved after.\n"
    report = run_slop(body, path="code2.md")
    # Both real occurrences of "delved" contain "delve" only as a substring,
    # not a whole-word match, but the literal word "delve" does not appear
    # outside the fence here, so no lexicon:word finding should fire for it;
    # instead assert offsets for any finding found are correct.
    for f in report.findings:
        if f.span is not None:
            assert body[f.span.start:f.span.end] == f.quote


# ---------------------------------------------------------------------------
# Scoring behavior: monotonicity, short-doc damping, config overrides
# ---------------------------------------------------------------------------


def test_scoring_monotonic_in_lexicon_density() -> None:
    base = "The morning was quiet, and nothing much happened before lunch. "
    one_tell = base + "She had to delve into the matter. "
    many_tells = base + "She had to delve into the matter, delve into the tapestry, delve into the testament. " * 3

    r0 = run_slop(base * 20, path="a.md")
    r1 = run_slop(one_tell * 20, path="b.md")
    r2 = run_slop(many_tells * 20, path="c.md")
    assert r0.score <= r1.score <= r2.score


def test_short_doc_damping_suppresses_score() -> None:
    short_slop = "---\ntitle: x\n---\n\nShe couldn't help but delve into the tapestry, a testament to it all.\n"
    long_slop = "---\ntitle: x\n---\n\n" + (
        "She couldn't help but delve into the tapestry, a testament to it all. "
        "It was not just survival, but redemption, and little did she know. "
    ) * 40

    short_report = run_slop(short_slop, path="short.md")
    long_report = run_slop(long_slop, path="long.md")
    assert short_report.stats["word_count"] < 200
    assert long_report.stats["word_count"] >= 200
    # Same relative density of tells, but the short doc's score is damped.
    assert short_report.score < long_report.score


def test_config_weights_override_changes_score() -> None:
    heavy_lexicon = SlopConfig(weights={
        "lexicon": 1.0, "phrases": 0.0, "patterns": 0.0,
        "punctuation": 0.0, "repetition": 0.0, "rhythm": 0.0, "density": 0.0,
    })
    default_report = run_slop(SLOPPY_TEXT, path="sloppy.md")
    custom_report = run_slop(SLOPPY_TEXT, path="sloppy.md", config=heavy_lexicon)
    assert default_report.score != custom_report.score


def test_empty_document_scores_zero() -> None:
    report = run_slop("", path="empty.md")
    assert report.score == 0.0
    assert report.findings == []


# ---------------------------------------------------------------------------
# Sentence splitter sanity (used by rhythm/repetition analyzers)
# ---------------------------------------------------------------------------


def test_split_sentences_handles_abbreviations_reasonably() -> None:
    text = "Dr. Smith arrived early. He nodded at Mrs. Jones. Then he left."
    spans = split_sentences(text)
    sentences = [text[s:e] for s, e in spans]
    assert len(sentences) == 3
    assert sentences[0].startswith("Dr. Smith")


def test_split_sentences_spans_are_exact_slices() -> None:
    text = "One two three. Four five six! Seven eight nine?"
    for s, e in split_sentences(text):
        assert text[s:e]  # non-empty, valid slice


# ---------------------------------------------------------------------------
# Report rendering
# ---------------------------------------------------------------------------


def test_render_json_roundtrips() -> None:
    import json

    report = run_slop(SLOPPY_TEXT, path="sloppy.md")
    payload = json.loads(render(report, "json"))
    assert payload["path"] == "sloppy.md"
    assert "score" in payload


def test_render_markdown_contains_verdict_and_findings() -> None:
    report = run_slop(SLOPPY_TEXT, path="sloppy.md")
    text = render(report, "markdown")
    assert "Score:" in text
    assert "Findings" in text


def test_render_rich_produces_text() -> None:
    report = run_slop(CLEAN_TEXT, path="clean.md")
    text = render(report, "rich")
    assert "Slop report" in text
    assert "Subscores" in text


def test_render_unknown_format_raises() -> None:
    report = run_slop(CLEAN_TEXT, path="clean.md")
    with pytest.raises(ValueError):
        render(report, "yaml")


def test_verdict_bands() -> None:
    assert verdict(0) == "clean"
    assert verdict(14.9) == "clean"
    assert verdict(15) == "touched up"
    assert verdict(29.9) == "touched up"
    assert verdict(30) == "slop-adjacent"
    assert verdict(54.9) == "slop-adjacent"
    assert verdict(55) == "slop"
    assert verdict(100) == "slop"
