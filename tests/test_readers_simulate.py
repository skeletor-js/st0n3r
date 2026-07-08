"""Tests for the chapter simulation pipeline (U3). Zero network."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import pytest

from stoner.canon.scaffold import scaffold_project
from stoner.ledger import Ledger
from stoner.project import WritingProject
from stoner.providers.base import Provider
from stoner.readers.simulate import run_readers
from stoner.readers.state import list_runs, load_state
from stoner.types import CompletionRequest, CompletionResponse, Usage

CH1 = (
    "The strongbox sat between them on the table.\n\n"
    "Mara counted the coins twice, and the silence stretched until it hummed.\n\n"
    "Outside, the frost had taken the last of the roses.\n"
)
CH2 = (
    "By morning the road had washed out below the bridge.\n\n"
    "Holt read the forged letter aloud, and no one moved.\n\n"
    "The dog would not stop barking at the dark.\n"
)

ROSTER = ["maya_riven", "owen_shelby", "marcus_hale", "lena_voss"]


class ScriptedProvider(Provider):
    """Replays scripted responses; records every request for assertions."""

    name = "scripted"

    def __init__(self, responder: Callable[[int, CompletionRequest], CompletionResponse]):
        self.responder = responder
        self.requests: list[CompletionRequest] = []

    def complete(self, req: CompletionRequest) -> CompletionResponse:
        idx = len(self.requests)
        self.requests.append(req)
        return self.responder(idx, req)


def _reader_payload(persona_ids: list[str], chapter: int, quote: str) -> str:
    readers = {
        pid: {
            "markers": [{"type": "hooked", "quote": quote, "note": "pulled in"}],
            "memory": f"{pid} recalls chapter {chapter}",
            "expectations": ["what happens next"],
            "fatigue": "",
        }
        for pid in persona_ids
    }
    return json.dumps({"readers": readers, "summary": "roster agreed"})


def _quote_for(req: CompletionRequest) -> str:
    """Pick a quote that is verbatim in whichever chapter the prompt carries."""
    body = req.messages[0].content
    return "strongbox" if "strongbox" in body else "forged letter"


def _persona_ids_in(req: CompletionRequest, roster: list[str]) -> list[str]:
    body = req.messages[0].content
    return [pid for pid in roster if f"id: {pid}" in body]


@pytest.fixture()
def project(tmp_path: Path) -> WritingProject:
    proj = WritingProject.create(tmp_path / "book", "Book")
    scaffold_project(proj, "Book")
    proj.write_chapter(1, {"title": "One", "pov": "Mara"}, CH1)
    proj.write_chapter(2, {"title": "Two", "pov": "Holt"}, CH2)
    return proj


# ---------------------------------------------------------------------------
# happy path
# ---------------------------------------------------------------------------


def test_happy_path_four_calls_markers_and_ledger(project: WritingProject):
    project.config.readers.personas_per_call = 2

    def responder(idx, req):
        ids = _persona_ids_in(req, ROSTER)
        chapter = 1 if "strongbox" in req.messages[0].content else 2
        return CompletionResponse(
            text=_reader_payload(ids, chapter, _quote_for(req)),
            usage=Usage(input_tokens=10, output_tokens=5),
        )

    provider = ScriptedProvider(responder)
    result = run_readers(project, roster=ROSTER, provider=provider)

    # 2 chapters x 2 batches (k=2, 4 personas) = 4 calls.
    assert result.calls == 4
    assert len(provider.requests) == 4
    assert result.chapters_read == 2
    # every persona marked both chapters -> 4 personas x 2 chapters = 8 markers.
    assert result.markers == 8
    assert result.misses == 0

    state = load_state(project, result.run_id)
    # markers resolved to real spans in the chapter body.
    for log in state.chapter_logs.values():
        for m in log.markers:
            assert m.span is not None and m.span.start >= 0

    # reader memory and expectations rolled forward across chapters.
    st = state.reader_states["owen_shelby"]
    assert "[ch 1]" in st.memory and "[ch 2]" in st.memory
    assert st.expectations == ["what happens next"]

    actions = [e.action for e in Ledger(project.root).tail(50)]
    assert actions.count("readers.run.start") == 1
    assert actions.count("readers.run.call") == 4
    assert actions.count("readers.run.done") == 1


# ---------------------------------------------------------------------------
# budget cap + resume
# ---------------------------------------------------------------------------


def test_budget_cap_stops_and_resume_completes(project: WritingProject):
    project.config.readers.personas_per_call = 2

    def responder(idx, req):
        ids = _persona_ids_in(req, ROSTER)
        chapter = 1 if "strongbox" in req.messages[0].content else 2
        return CompletionResponse(
            text=_reader_payload(ids, chapter, _quote_for(req)),
            usage=Usage(input_tokens=1, output_tokens=1),
        )

    p1 = ScriptedProvider(responder)
    r1 = run_readers(project, roster=ROSTER, provider=p1, max_calls=2)
    assert r1.stopped_at_cap is True
    assert r1.chapters_read == 1  # only chapter 1 completed (2 batches)
    assert p1.requests and all("strongbox" in q.messages[0].content for q in p1.requests)

    p2 = ScriptedProvider(responder)
    r2 = run_readers(project, provider=p2, resume=True, max_calls=10)
    assert r2.run_id == r1.run_id
    assert r2.chapters_read == 1  # chapter 2 only; chapter 1 not re-read
    assert all("forged letter" in q.messages[0].content for q in p2.requests)

    state = load_state(project, r1.run_id)
    assert set(state.chapter_logs) == {1, 2}
    assert len(list_runs(project)) == 1


# ---------------------------------------------------------------------------
# degradation
# ---------------------------------------------------------------------------


def test_garbage_batch_degrades_to_miss(project: WritingProject):
    project.config.readers.personas_per_call = 4  # one batch per chapter

    def responder(idx, req):
        if "strongbox" in req.messages[0].content:
            return CompletionResponse(text="not json at all", usage=Usage())
        ids = _persona_ids_in(req, ROSTER)
        return CompletionResponse(text=_reader_payload(ids, 2, _quote_for(req)), usage=Usage())

    result = run_readers(project, roster=ROSTER, provider=ScriptedProvider(responder))
    assert result.chapters_read == 2  # run completes despite the bad batch
    assert result.misses == 4  # chapter 1's whole batch missed
    assert any("unparseable" in n for n in result.notes)

    state = load_state(project, result.run_id)
    assert set(state.chapter_logs[1].misses) == set(ROSTER)
    assert state.chapter_logs[1].markers == []
    assert state.chapter_logs[2].markers  # chapter 2 still logged


def test_marker_quote_not_in_body_kept_with_null_span(project: WritingProject):
    project.config.readers.personas_per_call = 4

    def responder(idx, req):
        ids = _persona_ids_in(req, ROSTER)
        readers = {
            pid: {
                "markers": [{"type": "confused", "quote": "THIS PHRASE IS NOWHERE", "note": "?"}],
                "memory": "hm",
                "expectations": [],
                "fatigue": "",
            }
            for pid in ids
        }
        return CompletionResponse(text=json.dumps({"readers": readers}), usage=Usage())

    result = run_readers(project, roster=ROSTER, chapters=[1], provider=ScriptedProvider(responder))
    state = load_state(project, result.run_id)
    markers = state.chapter_logs[1].markers
    assert markers and all(m.span is None for m in markers)
    assert all(m.type == "confused" for m in markers)


# ---------------------------------------------------------------------------
# crash safety + determinism
# ---------------------------------------------------------------------------


def test_state_valid_after_interrupt_saved_before_failing_call(project: WritingProject):
    project.config.readers.personas_per_call = 4

    class Boom(RuntimeError):
        pass

    def responder(idx, req):
        raise Boom("provider exploded")

    with pytest.raises(Boom):
        run_readers(project, roster=ROSTER, chapters=[1], provider=ScriptedProvider(responder))

    # state saved before the failing call: exactly one run dir, and it parses.
    runs = list_runs(project)
    assert len(runs) == 1
    assert runs[0].budget.calls_made == 1  # the intent of the failing call persisted


def test_determinism_identical_chapter_logs(project: WritingProject):
    project.config.readers.personas_per_call = 2

    def responder(idx, req):
        ids = _persona_ids_in(req, ROSTER)
        chapter = 1 if "strongbox" in req.messages[0].content else 2
        return CompletionResponse(text=_reader_payload(ids, chapter, _quote_for(req)), usage=Usage())

    a = run_readers(project, roster=ROSTER, provider=ScriptedProvider(responder))
    # wipe the run dir and go again with the same scripted inputs.
    import shutil

    shutil.rmtree(project.root / ".stoner" / "readers")
    b = run_readers(project, roster=ROSTER, provider=ScriptedProvider(responder))

    la = load_state(project, a.run_id).chapter_logs
    lb = load_state(project, b.run_id).chapter_logs

    def norm(logs):
        return {k: v.model_dump() for k, v in logs.items()}

    assert norm(la) == norm(lb)
