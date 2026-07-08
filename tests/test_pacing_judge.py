"""Tests for the pacing judge (U3): prompt purity, tolerant parsing,
hash-keyed caching, resumable state, and the one-chapter-per-call
invariant. All offline via a scripted FakeProvider."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import pytest

from stoner.canon.memory import Memory
from stoner.canon.scaffold import scaffold_project
from stoner.ledger import Ledger
from stoner.pacing.data import assemble_chapters
from stoner.pacing.judge import (
    build_judge_prompt,
    judge_chapters,
    load_state,
    parse_judgment,
    save_state,
    state_path,
)
from stoner.project import WritingProject
from stoner.providers.base import Provider, ProviderError
from stoner.types import CompletionRequest, CompletionResponse, Usage

CH_BODIES = {
    1: "UNIQUEBODYONE Mara counted the crates on the dock while the tide came in.\n",
    2: "UNIQUEBODYTWO Holt broke the seal and read the letter twice before he spoke.\n",
    3: "UNIQUEBODYTHREE The granary door hung open and the grain was simply gone.\n",
}


class FakeProvider(Provider):
    """Scripted provider (mirrors tests/test_review.py)."""

    name = "fake"

    def __init__(self, responses: list | Callable[[int], CompletionResponse]):
        self.responses = responses
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


def _judgment_json(tension: str, changes=None, beats=None) -> str:
    return "```json\n" + json.dumps(
        {
            "tension": tension,
            "tension_why": "because",
            "changes_hands": changes or [],
            "beats": beats or [],
        }
    ) + "\n```"


def _response(tension: str, changes=None, beats=None, tokens: int = 10) -> CompletionResponse:
    return CompletionResponse(
        text=_judgment_json(tension, changes, beats),
        usage=Usage(input_tokens=tokens, output_tokens=tokens // 2),
    )


@pytest.fixture()
def project(tmp_path: Path) -> WritingProject:
    proj = WritingProject.create(tmp_path / "book", "Judge Book")
    scaffold_project(proj, "Judge Book")
    memory = Memory(proj)
    for n, body in CH_BODIES.items():
        proj.write_chapter(n, {"title": f"Ch{n}", "pov": "Mara"}, body)
        memory.set_chapter_summary(n, f"SUMMARY-OF-CHAPTER-{n}", pov="Mara")
    return proj


# -- parse_judgment -----------------------------------------------------------


def test_parse_judgment_valid_labels_and_beats():
    text = _judgment_json(
        "rises",
        changes=["the deed passes to Holt"],
        beats=[{"beat": "granary falls", "verdict": "landed", "note": "ok"}],
    )
    j = parse_judgment(text, chapter=2)
    assert j is not None
    assert j.tension == "rises"
    assert j.changes_hands == ["the deed passes to Holt"]
    assert j.beats[0].verdict == "landed"


def test_parse_judgment_opens_only_valid_for_first_chapter():
    assert parse_judgment(_judgment_json("opens"), chapter=1, first=True) is not None
    assert parse_judgment(_judgment_json("opens"), chapter=2, first=False) is None


def test_parse_judgment_rejects_numeric_or_unknown_tension():
    assert parse_judgment(_judgment_json("7"), chapter=2) is None
    assert parse_judgment(_judgment_json("spikes"), chapter=2) is None
    assert parse_judgment("not json at all", chapter=2) is None


def test_parse_judgment_skips_invalid_beat_verdicts():
    text = _judgment_json(
        "holds",
        beats=[
            {"beat": "a", "verdict": "landed"},
            {"beat": "b", "verdict": "9/10"},
        ],
    )
    j = parse_judgment(text, chapter=2)
    assert j is not None
    assert [b.verdict for b in j.beats] == ["landed"]


# -- prompt content (invariant 4) ------------------------------------------------


def test_prompt_contains_only_one_chapter_body(project: WritingProject):
    chapters = assemble_chapters(project)
    ch2 = chapters[1]
    _system, user = build_judge_prompt(ch2, prev_summary=chapters[0].memory_summary)
    assert "UNIQUEBODYTWO" in user
    assert "UNIQUEBODYONE" not in user  # previous chapter is summary only
    assert "UNIQUEBODYTHREE" not in user
    assert "SUMMARY-OF-CHAPTER-1" in user


def test_prompt_includes_beat_sheet_and_first_chapter_note(project: WritingProject):
    chapters = assemble_chapters(project)
    _system, user = build_judge_prompt(chapters[0], prev_summary="")
    assert "first chapter" in user.lower()
    # scaffold ships a beat sheet for chapter 1
    assert "## Beat Sheet" in user


def test_judge_never_sends_two_chapter_bodies_in_any_request(project: WritingProject):
    provider = FakeProvider(lambda i: _response("rises" if i else "opens"))
    judge_chapters(project, assemble_chapters(project), provider=provider)
    assert len(provider.requests) == 3
    for req, expected in zip(provider.requests, [1, 2, 3], strict=True):
        content = req.messages[0].content
        for n, marker in {1: "UNIQUEBODYONE", 2: "UNIQUEBODYTWO", 3: "UNIQUEBODYTHREE"}.items():
            assert (marker in content) == (n == expected)


# -- happy path / degradation -----------------------------------------------------


def test_judge_happy_path_labels_beats_and_usage(project: WritingProject):
    responses = [
        _response("opens", tokens=100),
        _response("rises", changes=["Mara learns the truth"], tokens=80),
        _response(
            "sags",
            beats=[{"beat": "the theft lands", "verdict": "drifted", "note": ""}],
            tokens=60,
        ),
    ]
    provider = FakeProvider(responses)
    judgments, findings, usage = judge_chapters(project, assemble_chapters(project), provider=provider)

    assert [j.tension for j in judgments] == ["opens", "rises", "sags"]
    assert judgments[1].changes_hands == ["Mara learns the truth"]
    assert judgments[2].beats[0].verdict == "drifted"
    assert findings == []
    assert usage.input_tokens == 240
    assert usage.output_tokens == 120
    assert all(not j.cached for j in judgments)


def test_judge_garbage_response_degrades_only_that_chapter(project: WritingProject):
    responses = [
        _response("opens"),
        # Chapter 2's first attempt AND its format-correction retry both fail,
        # so it degrades to unjudged.
        CompletionResponse(text="sorry, I cannot produce JSON today", usage=Usage()),
        CompletionResponse(text="sorry, I cannot produce JSON today", usage=Usage()),
        _response("rises"),
    ]
    judgments, findings, _usage = judge_chapters(
        project, assemble_chapters(project), provider=FakeProvider(responses)
    )
    assert [j.tension for j in judgments] == ["opens", "unjudged", "rises"]
    assert len(findings) == 1
    assert findings[0].severity.value == "info"
    assert findings[0].category == "ch-02:unjudged"


def test_judge_provider_error_degrades_not_aborts(project: WritingProject):
    responses = [_response("opens"), ProviderError("rate limited"), _response("rises")]
    judgments, findings, _usage = judge_chapters(
        project, assemble_chapters(project), provider=FakeProvider(responses)
    )
    assert [j.tension for j in judgments] == ["opens", "unjudged", "rises"]
    assert "rate limited" in findings[0].issue


# -- caching / state ------------------------------------------------------------------


def test_second_run_with_unchanged_chapters_makes_zero_calls(project: WritingProject):
    chapters = assemble_chapters(project)
    first = FakeProvider(lambda i: _response("rises" if i else "opens"))
    judge_chapters(project, chapters, provider=first)
    assert len(first.requests) == 3

    second = FakeProvider(lambda i: _response("sags"))
    judgments, _findings, usage = judge_chapters(project, chapters, provider=second)
    assert second.requests == []
    assert all(j.cached for j in judgments)
    assert [j.tension for j in judgments] == ["opens", "rises", "rises"]
    assert usage.input_tokens == 0

    tail = Ledger(project.root).tail(10)
    cached_entries = [e for e in tail if e.action == "pacing.judge" and e.detail.get("cached")]
    assert len(cached_entries) == 3


def test_editing_one_chapter_rejudges_only_that_chapter(project: WritingProject):
    judge_chapters(
        project,
        assemble_chapters(project),
        provider=FakeProvider(lambda i: _response("rises" if i else "opens")),
    )
    project.write_chapter(2, {"title": "Ch2", "pov": "Mara"}, "UNIQUEBODYTWO rewritten from scratch.\n")

    provider = FakeProvider([_response("sags")])
    judgments, _findings, _usage = judge_chapters(
        project, assemble_chapters(project), provider=provider
    )
    assert len(provider.requests) == 1
    assert "rewritten from scratch" in provider.requests[0].messages[0].content
    assert [j.cached for j in judgments] == [True, False, True]
    assert judgments[1].tension == "sags"


def test_unjudged_results_are_not_cached(project: WritingProject):
    responses = [
        _response("opens"),
        # Both the first attempt and the retry fail, so chapter 2 stays unjudged.
        CompletionResponse(text="garbage", usage=Usage()),
        CompletionResponse(text="garbage", usage=Usage()),
        _response("rises"),
    ]
    judge_chapters(project, assemble_chapters(project), provider=FakeProvider(responses))

    retry = FakeProvider([_response("holds")])
    judgments, findings, _usage = judge_chapters(
        project, assemble_chapters(project), provider=retry
    )
    assert len(retry.requests) == 1  # only the previously-unjudged chapter
    assert judgments[1].tension == "holds"
    assert findings == []


# -- format-correction retry ----------------------------------------------------------


def _garbage(tokens: int = 4) -> CompletionResponse:
    return CompletionResponse(
        text="sorry, I cannot produce JSON today",
        usage=Usage(input_tokens=tokens, output_tokens=tokens // 2),
    )


def test_retry_garbage_then_valid_is_judged_cached_and_flagged(project: WritingProject):
    chapters = assemble_chapters(project)[:1]
    provider = FakeProvider([_garbage(tokens=4), _response("opens", tokens=100)])
    judgments, findings, usage = judge_chapters(project, chapters, provider=provider)

    # First-try success semantics after a retry: judged, no finding.
    assert [j.tension for j in judgments] == ["opens"]
    assert findings == []
    # Exactly two provider calls; the second carries the correction.
    assert len(provider.requests) == 2
    assert "STRICT JSON only" in provider.requests[1].messages[0].content
    # Usage accumulates across BOTH calls.
    assert usage.input_tokens == 104
    assert usage.output_tokens == 52

    # Ledger detail flags the retry on the (cached=false) success entry.
    entry = next(
        e for e in Ledger(project.root).tail(10)
        if e.action == "pacing.judge" and not e.detail.get("cached")
    )
    assert entry.detail.get("retried") is True

    # A judged-after-retry result is cached: a second run makes zero calls.
    second = FakeProvider([_response("sags")])
    judgments2, _f2, _u2 = judge_chapters(project, chapters, provider=second)
    assert second.requests == []
    assert judgments2[0].cached is True
    assert judgments2[0].tension == "opens"


def test_retry_garbage_then_garbage_degrades_not_cached(project: WritingProject):
    chapters = assemble_chapters(project)[:1]
    provider = FakeProvider([_garbage(), _garbage()])
    judgments, findings, _usage = judge_chapters(project, chapters, provider=provider)

    assert judgments[0].tension == "unjudged"
    assert len(provider.requests) == 2  # one retry, then degrade
    assert len(findings) == 1
    assert findings[0].severity.value == "info"
    assert findings[0].category == "ch-01:unjudged"

    # Not cached: a re-run pays for the chapter again.
    retry = FakeProvider([_response("opens")])
    judgments2, findings2, _u2 = judge_chapters(project, chapters, provider=retry)
    assert len(retry.requests) == 1
    assert judgments2[0].tension == "opens"
    assert findings2 == []


def test_valid_first_try_makes_no_retry(project: WritingProject):
    chapters = assemble_chapters(project)[:1]
    provider = FakeProvider([_response("opens")])
    judgments, findings, _usage = judge_chapters(project, chapters, provider=provider)

    assert len(provider.requests) == 1  # no retry
    assert judgments[0].tension == "opens"
    assert findings == []
    entry = next(
        e for e in Ledger(project.root).tail(10)
        if e.action == "pacing.judge" and not e.detail.get("cached")
    )
    assert entry.detail.get("retried") is False


def test_cache_hit_makes_zero_calls_even_with_retry_path(project: WritingProject):
    chapters = assemble_chapters(project)[:1]
    judge_chapters(project, chapters, provider=FakeProvider([_response("opens")]))

    second = FakeProvider([_garbage(), _response("rises")])
    judgments, _findings, usage = judge_chapters(project, chapters, provider=second)
    assert second.requests == []  # cache hit short-circuits before any call
    assert judgments[0].cached is True
    assert usage.input_tokens == 0


def test_corrupt_state_file_moves_to_bak_and_run_completes(project: WritingProject):
    p = state_path(project)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("{definitely not json", encoding="utf-8")

    provider = FakeProvider(lambda i: _response("rises" if i else "opens"))
    judgments, _findings, _usage = judge_chapters(
        project, assemble_chapters(project), provider=provider
    )
    assert len(judgments) == 3
    assert (p.parent / (p.name + ".bak")).exists()
    assert p.exists()  # fresh state written


def test_state_saved_before_every_model_call(project: WritingProject):
    """The state file must exist by the time the first provider call runs."""
    chapters = assemble_chapters(project)
    seen: list[bool] = []

    def scripted(i: int) -> CompletionResponse:
        seen.append(state_path(project).exists())
        return _response("rises" if i else "opens")

    judge_chapters(project, chapters, provider=FakeProvider(scripted))
    assert seen and all(seen)


def test_state_round_trips(project: WritingProject):
    provider = FakeProvider(lambda i: _response("rises" if i else "opens"))
    judge_chapters(project, assemble_chapters(project), provider=provider)
    state = load_state(project)
    assert set(state.chapters) == {1, 2, 3}
    save_state(project, state)
    assert set(load_state(project).chapters) == {1, 2, 3}
