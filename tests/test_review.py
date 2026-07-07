"""Tests for the review engine: passes, runner, and revise.

No network calls: a scripted FakeProvider stands in for a real vendor
backend, mirroring the pattern in `tests/test_engine.py`.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import pytest

from stoner.canon.scaffold import scaffold_project
from stoner.ledger import Ledger
from stoner.project import WritingProject, count_words
from stoner.providers.base import Provider, ProviderError
from stoner.review.passes import extract_json, locate_span
from stoner.review.revise import ReviseResult, revise_chapter
from stoner.review.runner import run_review
from stoner.types import CompletionRequest, CompletionResponse, Finding, Severity, Usage

# ---------------------------------------------------------------------------
# fixtures / helpers
# ---------------------------------------------------------------------------

CH1_BODY = (
    "Aria pressed her palm to the cold door.\n"
    "It refused to move.\n\n"
    "She had felt this fear before, in the tunnels beneath Cadmus.\n\n"
    '"I will not go back," she said.\n'
)


@pytest.fixture
def project(tmp_path: Path) -> WritingProject:
    proj = WritingProject.create(tmp_path / "book", "Test Book")
    scaffold_project(proj, "Test Book")
    proj.write_chapter(1, {"title": "The Door", "pov": "Aria"}, CH1_BODY)
    return proj


class FakeProvider(Provider):
    """Scripted provider: a fixed list of responses/exceptions (last one
    repeats once exhausted), or a callable `(turn_index) -> CompletionResponse`."""

    name = "fake"

    def __init__(self, responses: list | Callable[[int], CompletionResponse], supports_tools: bool = True):
        self.responses = responses
        self.supports_tools = supports_tools
        self.requests: list[CompletionRequest] = []

    def complete(self, req: CompletionRequest) -> CompletionResponse:
        idx = len(self.requests)
        self.requests.append(req)
        if callable(self.responses):
            item = self.responses(idx)
        else:
            item = self.responses[min(idx, len(self.responses) - 1)]
        if isinstance(item, Exception):
            raise item
        return item


def _json_response(payload: dict) -> str:
    return "```json\n" + json.dumps(payload) + "\n```"


# ---------------------------------------------------------------------------
# extract_json
# ---------------------------------------------------------------------------


def test_extract_json_fenced_with_json_tag():
    text = 'Sure, here you go:\n```json\n{"a": 1}\n```\nHope that helps!'
    assert extract_json(text) == {"a": 1}


def test_extract_json_fenced_without_tag():
    text = '```\n{"a": 2}\n```'
    assert extract_json(text) == {"a": 2}


def test_extract_json_raw_with_trailing_prose():
    text = '{"a": 3}\n\nLet me know if you would like further changes.'
    assert extract_json(text) == {"a": 3}

def test_extract_json_raw_no_wrapping():
    assert extract_json('{"findings": [], "summary": "clean"}') == {"findings": [], "summary": "clean"}


def test_extract_json_unparseable_returns_empty():
    assert extract_json("no json here at all, sorry") == {}


def test_extract_json_empty_string_returns_empty():
    assert extract_json("") == {}
    assert extract_json("   ") == {}


# ---------------------------------------------------------------------------
# locate_span
# ---------------------------------------------------------------------------


def test_locate_span_finds_quote_and_correct_line():
    body = "Line one.\nLine two has the target phrase right here.\nLine three."
    quote = "target phrase right here"
    span = locate_span(body, quote)
    idx = body.find(quote)
    assert span is not None
    assert span.start == idx
    assert span.end == idx + len(quote)
    assert span.line == 2


def test_locate_span_missing_quote_returns_none():
    assert locate_span("some text here", "not present anywhere") is None


def test_locate_span_empty_quote_returns_none():
    assert locate_span("some text here", "") is None


# ---------------------------------------------------------------------------
# run_review: happy path
# ---------------------------------------------------------------------------


def test_run_review_two_passes_saves_report_and_locates_spans(project):
    body = project.read_chapter(1)[1]
    quote1 = "She had felt this fear before, in the tunnels beneath Cadmus."
    quote2 = "Aria pressed her palm to the cold door."
    assert quote1 in body and quote2 in body

    responses = [
        CompletionResponse(
            text=_json_response(
                {
                    "findings": [
                        {
                            "severity": "major",
                            "category": "fact",
                            "quote": quote1,
                            "issue": "The Cadmus tunnels are not established in canon.",
                            "suggestion": "Confirm against canon or cut the reference.",
                        }
                    ],
                    "summary": "One continuity concern.",
                }
            ),
            usage=Usage(input_tokens=100, output_tokens=40),
        ),
        CompletionResponse(
            text=_json_response(
                {
                    "findings": [
                        {
                            "severity": "minor",
                            "category": "filter-word",
                            "quote": quote2,
                            "issue": "Filter word distances the reader.",
                            "suggestion": "Cut the filter word.",
                        }
                    ],
                    "summary": "Some filter words present.",
                }
            ),
            usage=Usage(input_tokens=80, output_tokens=30),
        ),
    ]
    provider = FakeProvider(responses)

    report = run_review(project, chapter=1, passes=["continuity", "line"], model="fake/test-model", provider=provider)

    assert report.passes == ["continuity", "line"]
    assert len(report.findings) == 2
    assert report.usage.input_tokens == 180
    assert report.usage.output_tokens == 70
    assert report.model == "fake/test-model"
    assert "continuity" in report.summary and "line" in report.summary

    cont_finding = next(f for f in report.findings if f.source == "review:continuity")
    assert cont_finding.span is not None
    idx = body.find(quote1)
    assert cont_finding.span.start == idx
    assert cont_finding.span.end == idx + len(quote1)

    line_finding = next(f for f in report.findings if f.source == "review:line")
    assert line_finding.span is not None
    assert line_finding.span.start == body.find(quote2)

    review_dir = project.root / ".stoner" / "reviews"
    json_files = list(review_dir.glob("ch-01-*.json"))
    md_files = list(review_dir.glob("ch-01-*.md"))
    assert len(json_files) == 1
    assert len(md_files) == 1
    saved = json.loads(json_files[0].read_text(encoding="utf-8"))
    assert saved["kind"] == "review"
    assert len(saved["findings"]) == 2
    assert "review:continuity" in md_files[0].read_text(encoding="utf-8")

    entries = Ledger(project.root).tail(5)
    run_entries = [e for e in entries if e.action == "review.run"]
    assert len(run_entries) == 1
    assert run_entries[0].target == "manuscript/ch-01.md"


def test_run_review_defaults_to_config_passes_and_model(project):
    provider = FakeProvider(
        lambda idx: CompletionResponse(
            text=_json_response({"findings": [], "summary": f"pass {idx} clean"}),
            usage=Usage(input_tokens=1, output_tokens=1),
        )
    )
    report = run_review(project, chapter=1, provider=provider)

    assert report.passes == project.config.review_passes
    assert report.model == project.config.models.reviewer
    assert len(provider.requests) == len(project.config.review_passes)


# ---------------------------------------------------------------------------
# run_review: failure tolerance
# ---------------------------------------------------------------------------


def test_run_review_tolerates_provider_error_and_unparseable_output(project):
    responses = [
        ProviderError("rate limited"),
        CompletionResponse(text="not json at all, sorry about that", usage=Usage()),
        CompletionResponse(
            text=_json_response(
                {"findings": [{"severity": "minor", "category": "x", "quote": "", "issue": "ok", "suggestion": ""}], "summary": "fine"}
            ),
            usage=Usage(input_tokens=5, output_tokens=5),
        ),
    ]
    provider = FakeProvider(responses)

    report = run_review(project, chapter=1, passes=["continuity", "pacing", "voice"], model="fake/m", provider=provider)

    assert len(report.findings) == 3

    provider_error_finding = next(f for f in report.findings if f.source == "review:continuity")
    assert provider_error_finding.severity == Severity.info
    assert "pass failed" in provider_error_finding.issue

    unparseable_finding = next(f for f in report.findings if f.source == "review:pacing")
    assert unparseable_finding.severity == Severity.info
    assert "pass failed" in unparseable_finding.issue

    ok_finding = next(f for f in report.findings if f.source == "review:voice")
    assert ok_finding.issue == "ok"
    assert ok_finding.severity == Severity.minor


def test_run_review_unknown_pass_name_records_failure_without_provider_call(project):
    provider = FakeProvider([])
    report = run_review(project, chapter=1, passes=["not-a-real-pass"], model="fake/m", provider=provider)

    assert len(report.findings) == 1
    assert report.findings[0].severity == Severity.info
    assert "unknown pass" in report.findings[0].issue
    assert provider.requests == []


# ---------------------------------------------------------------------------
# panel consensus filtering
# ---------------------------------------------------------------------------


def test_panel_pass_filters_to_consensus(project):
    payload = {
        "personas": {
            "acquisitions_editor": "Strong hook.",
            "genre_reader": "Delivers on tension.",
            "rival_novelist": "Opening drags a little.",
            "first_time_reader": "Confusing pronoun in paragraph two.",
        },
        "consensus": [
            {"issue": "Opening is slow", "quote": "", "suggestion": "Cut first paragraph", "votes": 4},
            {"issue": "Dialogue tag overuse", "quote": "", "suggestion": "Vary tags", "votes": 3},
            {"issue": "Minor pronoun nit", "quote": "", "suggestion": "n/a", "votes": 1},
        ],
        "summary": "Panel mostly agrees the opening drags.",
    }
    provider = FakeProvider([CompletionResponse(text=_json_response(payload), usage=Usage())])

    report = run_review(project, chapter=1, passes=["panel"], model="fake/m", provider=provider)

    assert len(report.findings) == 3
    majors = [f for f in report.findings if f.severity == Severity.major]
    infos = [f for f in report.findings if f.severity == Severity.info]
    assert len(majors) == 2
    assert len(infos) == 1
    assert all(f.category == "consensus" for f in majors)
    assert infos[0].category == "minority"


# ---------------------------------------------------------------------------
# grade distribution stats
# ---------------------------------------------------------------------------


def test_grade_pass_produces_findings_and_distribution(project):
    payload = {
        "grades": [
            {"paragraph": 1, "quote": "Aria pressed", "label": "STRONG", "reason": "good hook"},
            {"paragraph": 2, "quote": "She had felt", "label": "WEAK", "reason": "filter word"},
            {"paragraph": 3, "quote": "I will not go back", "label": "CUT", "reason": "redundant beat"},
            {"paragraph": 4, "quote": "It refused", "label": "FINE", "reason": "adequate"},
        ],
        "summary": "Mixed chapter.",
    }
    provider = FakeProvider([CompletionResponse(text=_json_response(payload), usage=Usage())])

    report = run_review(project, chapter=1, passes=["grade"], model="fake/m", provider=provider)

    weak = [f for f in report.findings if f.category == "WEAK"]
    cut = [f for f in report.findings if f.category == "CUT"]
    dist = [f for f in report.findings if f.category == "distribution"]

    assert len(weak) == 1 and weak[0].severity == Severity.minor
    assert len(cut) == 1 and cut[0].severity == Severity.major
    assert len(dist) == 1 and dist[0].severity == Severity.info
    for label_count in ("STRONG: 1", "FINE: 1", "WEAK: 1", "CUT: 1"):
        assert label_count in dist[0].issue


# ---------------------------------------------------------------------------
# revise_chapter
# ---------------------------------------------------------------------------


def test_revise_chapter_end_to_end(project):
    old_body = project.read_chapter(1)[1]
    findings = [
        Finding(
            source="review:line",
            severity=Severity.minor,
            category="filter-word",
            quote="She had felt this fear before, in the tunnels beneath Cadmus.",
            issue="filter word 'felt'",
            suggestion="cut it",
        )
    ]
    revised_text = (
        "SUMMARY: Tightened the opening and cut the filter word.\n"
        "BEGIN CHAPTER\n"
        "Aria pressed her palm to the cold door. It refused to move.\n\n"
        "This fear was old, from the tunnels beneath Cadmus.\n\n"
        '"I will not go back," she said.\n'
        "END CHAPTER\n"
    )
    provider = FakeProvider([CompletionResponse(text=revised_text, usage=Usage(input_tokens=50, output_tokens=60))])

    result = revise_chapter(project, chapter=1, findings=findings, model="fake/m", provider=provider)

    assert isinstance(result, ReviseResult)
    assert result.chapter == 1
    assert result.applied == 1
    assert result.old_words == count_words(old_body)
    assert "Tightened the opening" in result.summary
    assert result.usage.input_tokens == 50
    assert result.usage.output_tokens == 60

    fm, body = project.read_chapter(1)
    assert fm["status"] == "revised"
    assert fm["title"] == "The Door"
    assert fm["pov"] == "Aria"
    assert "This fear was old" in body
    assert "felt this fear before" not in body
    assert result.new_words == count_words(body)
    assert fm["words"] == result.new_words

    entries = Ledger(project.root).tail(5)
    assert any(e.action == "review.revise" for e in entries)


def test_revise_chapter_requires_findings(project):
    with pytest.raises(ValueError):
        revise_chapter(project, chapter=1, findings=[], model="fake/m", provider=FakeProvider([]))


def test_revise_chapter_tolerant_of_missing_end_sentinel(project):
    findings = [Finding(source="review:line", severity=Severity.minor, issue="x")]
    text = "SUMMARY: quick fix\nBEGIN CHAPTER\nJust the revised prose without an end marker.\n"
    provider = FakeProvider([CompletionResponse(text=text, usage=Usage())])

    revise_chapter(project, chapter=1, findings=findings, model="fake/m", provider=provider)

    _, body = project.read_chapter(1)
    assert "Just the revised prose without an end marker." in body


def test_revise_chapter_tolerant_of_missing_sentinels_entirely(project):
    findings = [Finding(source="review:line", severity=Severity.minor, issue="x")]
    text = "Plain revised chapter text with no wrapper at all."
    provider = FakeProvider([CompletionResponse(text=text, usage=Usage())])

    result = revise_chapter(project, chapter=1, findings=findings, model="fake/m", provider=provider)

    _, body = project.read_chapter(1)
    assert body.strip() == text.strip()
    assert result.summary == f"Applied {len(findings)} finding(s) to chapter {result.chapter}."
